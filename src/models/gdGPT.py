"""
A (heavily) modified version of karpathy's nanoGPT model (https://github.com/karpathy/nanoGPT)
"""
import math

import torch
from torch import nn
from torch.nn import functional as F

class GDAttention(nn.Module):

    def __init__(self, config):
        super().__init__()
        
        self.n_head = config.n_head
        self.d_embed = config.d_embed
        self.dropout = config.dropout
        
        # Only need W_o matrix for output projection
        self.W_o = nn.Linear(self.d_embed * self.n_head, self.d_embed, bias=config.bias)
        
        # Dropout
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
    
    def forward(self, e, p):
        B, S, _ = e.size()

        Q = p.repeat(1, 1, self.n_head).view(B, S + 1, self.n_head, self.d_embed).transpose(1, 2) # Use N+1 positional embeddings for query
        K = p[:, :-1, :].repeat(1, 1, self.n_head).view(B, S, self.n_head, self.d_embed).transpose(1, 2) # Only use first N positional embeddings for key
        V = e.repeat(1, 1, self.n_head).view(B, S, self.n_head, self.d_embed).transpose(1, 2)

        # This mask allows for causal attention while incorporating the N+1th query
        mask = torch.tril(torch.ones(S, S, device=e.device), diagonal=-1).view(1, S, S)
        mask = torch.cat([mask, torch.ones(1, 1, S, device=e.device)], dim=1)
        mask = mask.bool()
        
        y = torch.nn.functional.scaled_dot_product_attention(Q, K, V, attn_mask=mask, dropout_p=self.dropout if self.training else 0)
        y = y.transpose(1, 2).contiguous().view(B, S + 1, self.d_embed * self.n_head)
        
        y = y[:, 1:, :] # Use the outputs associated with the N+1th token, rather than Nth
        y = self.W_o(y)
        y = self.resid_dropout(y)
        
        return y

class Block(nn.Module):

    def __init__(self, config):
        super().__init__()
        
        self.use_ff = config.use_ff
      
        self.ln_p = nn.LayerNorm(config.d_embed, bias=config.bias)
        self.ln_e = nn.LayerNorm(config.d_embed, bias=config.bias)
        self.attn = GDAttention(config)
        
        if self.use_ff:
            self.ln_mlp = nn.LayerNorm(config.d_embed, bias=config.bias)
            self.mlp = nn.Sequential(
                nn.Linear(config.d_embed, config.d_ff, bias=config.bias),
                nn.GELU(),
                nn.Linear(config.d_ff, config.d_embed, bias=config.bias),
                nn.Dropout(config.dropout)
            )

    def forward(self, e, p):
        e = self.ln_e(e)
        p = self.ln_p(p)
        x = self.attn(e, p)
        if self.use_ff:
            x = x + self.mlp(self.ln_mlp(x))
        return x

class gdGPT(nn.Module):

    def __init__(self, config):
        super().__init__()
        
        self.config = config
        self.name = f'gdGPT_{config.n_head}H_{config.n_layer}L_{config.d_embed}E'
        
        if not config.use_ff:
            self.name += '_noFF'
        if not config.use_attn:
            self.name += '_noAttn'

        # Transformer Components
        self.wte = nn.Embedding(config.vocab_size, config.d_embed)
        self.wpe = nn.Embedding(config.context_size + 1, config.d_embed) # Need a positional vector for the N+1th token
        self.drop_p = nn.Dropout(config.dropout)
        self.drop_e = nn.Dropout(config.dropout)
        self.blocks = nn.ModuleList([Block(config) for _ in range(config.n_layer)])
        self.ln_f = nn.LayerNorm(config.d_embed, bias=config.bias)
        
        # LM Head
        self.lm_head = nn.Linear(config.d_embed, config.vocab_size, bias=False)
        self.wte.weight = self.lm_head.weight # Weight tying

        # Weight initialization
        self.apply(self._init_weights)
        for pn, p in self.named_parameters():
            if pn.endswith('c_proj.weight'):
                torch.nn.init.normal_(p, mean=0.0, std=0.02/math.sqrt(2 * config.n_layer))

        print("number of parameters: %.2fM" % (self.get_num_params()/1e6,))

    def get_num_params(self, non_embedding=True):
        n_params = sum(p.numel() for p in self.parameters())
        if non_embedding:
            n_params -= self.wpe.weight.numel()
        return n_params

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, idx, targets=None):
        
        device = idx.device
        B, S = idx.size()
        assert S <= self.config.context_size, f"Cannot forward sequence of length {S}, context size is only {self.context_size}"
        
        pos = torch.arange(0, S + 1, dtype=torch.long, device=device)

        e = self.wte(idx) # token embeddings of shape (B, S, d_embed)
        p = self.wpe(pos).repeat(B, 1, 1) # position embeddings of shape (B, S + 1, d_embed)

        e = self.drop_e(e)
        p = self.drop_p(p)
            
        for block in self.blocks:
            x = block(e, p)
        x = self.ln_f(x)

        if targets is not None:
            # if we are given some desired targets also calculate the loss
            logits = self.lm_head(x)
            targets = targets.contiguous()
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
        else:
            # inference-time mini-optimization: only forward the lm_head on the very last position
            logits = self.lm_head(x[:, [-1], :])
            loss = None

        return logits, loss
    
    @torch.no_grad()
    def generate(self, idx, max_new_tokens, temperature=1.0, top_k=None):
        """
        Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
        the sequence max_new_tokens times, feeding the predictions back into the model each time.
        Most likely you'll want to make sure to be in model.eval() mode of operation for this.
        """
        for _ in range(max_new_tokens):
            # if the sequence context is growing too long we must crop it at context_size
            idx_cond = idx if idx.size(1) <= self.config.context_size else idx[:, -self.config.context_size:]
            # forward the model to get the logits for the index in the sequence
            logits, _ = self(idx_cond)
            # pluck the logits at the final step and scale by desired temperature
            logits = logits[:, -1, :] / temperature
            # optionally crop the logits to only the top k options
            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float('Inf')
            # apply softmax to convert logits to (normalized) probabilities
            probs = F.softmax(logits, dim=-1)
            # sample from the distribution
            idx_next = torch.multinomial(probs, num_samples=1)
            # append sampled index to the running sequence and continue
            idx = torch.cat((idx, idx_next), dim=1)

        return idx