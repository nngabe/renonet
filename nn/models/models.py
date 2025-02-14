from typing import Any, Optional, Sequence, Tuple, Union, List
import copy

import manifolds
import layers.hyp_layers as hyp_layers
from layers.layers import GCNConv, Linear, KANLayer, get_dim_act
import utils.math_utils as pmath
import lib.function_spaces

import numpy as np
import jax
import jax.random as jr
import jax.numpy as jnp
import equinox as eqx
import equinox.nn as nn
from equinox import Module, static_field
from jaxtyping import Array, Float, PRNGKeyArray

prng = lambda i=0: jr.PRNGKey(i)

class GraphNet(eqx.Module):
    c: float
    res: bool
    cat: bool
    norm: bool
    layers: eqx.nn.Sequential
    lin: eqx.nn.Sequential = eqx.field(static=True)
    layer_norm: eqx.nn.Sequential
    encode_graph: bool
    manifold: Optional[manifolds.base.Manifold] = None
    pe_dim: int
    kappa: int
    euclidean: bool
    dropout: eqx.nn.Dropout
    censor: bool

    def __init__(self, args, module):
        super(GraphNet, self).__init__()
        self.c = args.c
        self.res = bool(args.res) 
        self.cat = True if module=='enc' else False
        self.norm = bool(args.use_layer_norm)
        self.pe_dim = args.pe_dim
        self.kappa = args.kappa
        self.euclidean = True if args.manifold=='Euclidean' else False
        self.dropout = eqx.nn.Dropout(args.dropout)
        self.censor = (args.enc_depth == 0)

    def exp(self, x):
        x = self.manifold.proj_tan0(x, c=self.c)
        x = self.manifold.expmap0(x, c=self.c)
        x = self.manifold.proj(x, c=self.c)
        return x

    def log(self, y):
        y = self.manifold.logmap0(y, self.c)
        return y

    def __call__(self, x, adj, key, w):
        x_i = [x[:,:self.kappa], x[:,self.kappa:]]
        x = self.exp(x)
        for conv,lin,norm in zip(self.layers, self.lin, self.layer_norm):
            h,_ = conv(x, adj, key, w)
            h = self.log(h)
            if self.res:
                x = jax.vmap(lin)(self.log(x)) + h
            if self.norm:
                x = jax.vmap(norm)(x)
            x_i.append(x)
            x = self.exp(x)
            key = jax.random.split(key)[0]
        if self.cat:
            res = jnp.concatenate([x_i[0], x_i[-1]], axis=-1) 
        else:
            res = self.log(x)
        return res
        

class AttentionBlock(eqx.Module):
    layer_norm1: eqx.nn.LayerNorm
    layer_norm2: eqx.nn.LayerNorm
    attention: eqx.nn.MultiheadAttention
    linear1: eqx.nn.Linear
    linear2: eqx.nn.Linear
    dropout1: eqx.nn.Dropout
    dropout2: eqx.nn.Dropout

    def __init__(
        self,
        input_shape: int,
        hidden_dim: int,
        num_heads: int,
        dropout_rate: float,
        key: PRNGKeyArray,
    ):
        keys = jr.split(key, 3)

        self.layer_norm1 = eqx.nn.LayerNorm(input_shape)
        self.layer_norm2 = eqx.nn.LayerNorm(input_shape)
        self.attention = eqx.nn.MultiheadAttention(num_heads, input_shape, key=keys[0], use_output_bias=False)

        self.linear1 = eqx.nn.Linear(input_shape, hidden_dim, key=keys[1])
        self.linear2 = eqx.nn.Linear(hidden_dim, input_shape, key=keys[2])
        self.dropout1 = eqx.nn.Dropout(dropout_rate)
        self.dropout2 = eqx.nn.Dropout(dropout_rate)

    def __call__(self, x, key, mask=None, inspect=False): 
        
        input_x = jax.vmap(self.layer_norm1)(x)
        attn = self.attention(input_x, input_x, input_x, mask=mask)
        x = x + attn 

        input_x = jax.vmap(self.layer_norm2)(x)
        input_x = jax.vmap(self.linear1)(input_x)
        input_x = jax.nn.gelu(input_x)

        keys = jr.split(key, 2)

        input_x = self.dropout1(input_x, key=keys[0])
        input_x = jax.vmap(self.linear2)(input_x)
        input_x = self.dropout2(input_x, key=keys[1])

        x = x + input_x
        
        if inspect: 
            return x,attn 
        else: 
            return x

class Transformer(eqx.Module):
    level_dims: list[int]
    level_embedding: jnp.ndarray
    positional_embedding: jnp.ndarray
    attention_blocks: list[AttentionBlock]
    dropout: eqx.nn.Dropout
    norm1: eqx.nn.LayerNorm
    lin1: eqx.nn.Linear
    lin2: eqx.nn.Linear
    num_layers: int
    eps: float
    pos_emb_var: list[float]
    level_emb_var: list[float]
    dual_embed: bool

    def __init__(self, args=None, in_dim=-1, out_dim=-1, key=prng(3)):
        
        keys = jr.split(key,10)
        hidden_dim = args.dec_width
        num_heads = args.num_heads
        dropout_rate = args.dropout_branch
        self.num_layers = args.dec_depth 
    
        level_dims = [0] + [args.batch_size] 
        if sum(args.pool_size) > 0: level_dims += args.pool_size 

        self.level_dims = np.array(level_dims).cumsum().tolist()
        self.level_embedding = jr.normal(keys[0], (len(args.pool_size) + 1, in_dim))
        self.positional_embedding = jr.normal(keys[1], (args.num_nodes , in_dim))
        self.attention_blocks = [AttentionBlock(hidden_dim, 4 * hidden_dim, num_heads, dropout_rate, keys[i]) for i in range(self.num_layers)]

        self.dropout = eqx.nn.Dropout(dropout_rate)
        self.norm1 = eqx.nn.LayerNorm(hidden_dim)
        self.lin1 = eqx.nn.Linear(in_dim, hidden_dim, key=keys[2])
        self.lin2 = eqx.nn.Linear(hidden_dim, out_dim, key=keys[3])
        self.eps = 1e-15
        self.pos_emb_var = args.pos_emb_var
        self.level_emb_var = args.level_emb_var
        self.dual_embed = args.dual_pos_emb

    def get_attn_mask(self, x):
        # filter out padding nodes that have x = [0., ..., 0.] 
        index = jnp.abs(x.sum(1)) > self.eps
        mask = jnp.einsum('i,j -> ij', index, index)
        return mask 
    
    def multiscale_embedding(self, x):
        res = jnp.zeros_like(x)
        pos_emb_scalers = jnp.ones((self.level_dims[-1],1))
        pos_emb_scalers = pos_emb_scalers.at[:self.level_dims[1]].mul(self.pos_emb_var[0])
        pos_emb_scalers = pos_emb_scalers.at[self.level_dims[1]:].mul(self.pos_emb_var[1])
        res = res.at[:].add(self.positional_embedding[:res.shape[0]] * pos_emb_scalers)
        for i,dim in enumerate(self.level_dims[:-1]):
            l1,l2 = dim, self.level_dims[i+1]
            level_emb_scaler = self.level_emb_var[0] 
            res = res.at[l1:l2].add(self.level_embedding[i] * level_emb_scaler)
        return res
    

    def __call__(self, x, key, pe=None, inspect=False):
        if inspect: attn = []
        mask = self.get_attn_mask(x) 
        if pe==None:
            x += self.multiscale_embedding(x)
        elif self.dual_embed: 
            x += pe + self.multiscale_embedding(x)
        else:
            x += pe
        dropout_key, *attention_keys = jr.split(key, num=self.num_layers + 1)
        x = jax.vmap(self.lin1)(x)
        x = self.dropout(x, key=dropout_key)
        for block, key in zip(self.attention_blocks, attention_keys):
            x = block(x, key=key, mask=mask, inspect=inspect)
            if inspect: 
                attn.append(x[1])
                x = x[0]
        x = jax.vmap(self.norm1)(x)
        x = jax.vmap(self.lin2)(x)

        if inspect: 
            return x,attn 
        else: 
            return x

class KAN(eqx.Module):
    num_layers: int
    res: bool
    layers: eqx.nn.Sequential 
    lin: eqx.nn.Sequential = eqx.field(static=True)

    def __init__(self, args=None, in_dim=-1, out_dim=-1, res=False, norm=False, module=None, shared=None, key=prng(4)):
        res = False
        if module:
            args, dims, _, _ = get_dim_act(args,module) 
            self.num_layers = len(dims)
        else: 
            dec_width = args.dec_width if args != None else out_dim//2
            num_layers = args.num_layers if args != None else 4
            hidden_dim = dec_width  #if args.branch_net=='Res' else args.dec_width * 2 
            dims = [in_dim] + num_layers * [hidden_dim] + [out_dim]
            self.num_layers = len(dims)
        
        keys = jr.split(key, self.num_layers + 2)
        self.res = res
        
        self.layers = [KANLayer(dims[i], dims[i+1]) for i in range(self.num_layers-1)]
        if self.res: 
            self.lin = [eqx.nn.Linear(dims[i], dims[i+1], key=keys[i]) for i in range(self.num_layers-1)]
        else: 
            #self.lin = [eqx.nn.Linear(dims[i], dims[i+1], key=keys[i]) if dims[i]!=dims[i+1] else (lambda x: x) for i in range(self.num_layers-1)]
            self.lin = [(lambda x: x) for i in range(self.num_layers-1)]
        self.layers = eqx.nn.Sequential(self.layers)
        self.lin = eqx.nn.Sequential(self.lin)    

    def __call__(self, x, key=prng(0), pe=None):

        if len(x.shape) < 2: 
            x = x.reshape(1,-1)
        for res,layer in zip(self.lin,self.layers):
            if self.res:
                f = lambda x: layer(x,key) + res(x.squeeze()).reshape(1,-1)
            else:
                f = lambda x: layer(x,key)
            x,_ = f(x)
            key = jr.split(key)[0]
        return x.squeeze()

class Res(eqx.Module):
    num_layers: int
    res: bool
    layers: eqx.nn.Sequential 
    lin: eqx.nn.Sequential = eqx.field(static=True)

    def __init__(self, args=None, in_dim=-1, out_dim=-1, res=True, norm=True, module=None, shared=None, key=prng(4)):
        if module:
            args, dims, _, _ = get_dim_act(args,module) 
            self.num_layers = len(dims)
        else: 
            hidden_dim = args.dec_width * 2 #if args.branch_net=='Res' else args.dec_width * 2 
            dims = [in_dim] + args.num_layers * [hidden_dim] + [out_dim]
            self.num_layers = len(dims)
        
        keys = jr.split(key, args.dec_depth + 2)
        self.res = res
        dropout_rate = args.dropout_trunk
        
        self.layers = [Linear(dims[i], dims[i+1], dropout_rate=dropout_rate, key=keys[i], norm=norm) for i in range(self.num_layers-1)]
        if args.lin_skip: 
            self.lin = [eqx.nn.Linear(dims[i], dims[i+1], key=keys[i]) for i in range(self.num_layers-1)]
        else: 
            self.lin = [eqx.nn.Linear(dims[i], dims[i+1], key=keys[i]) if dims[i]!=dims[i+1] else (lambda x: x) for i in range(self.num_layers-1)]
        self.layers = eqx.nn.Sequential(self.layers)
        self.lin = eqx.nn.Sequential(self.lin)    

    def __call__(self, x, key, pe=None):

        for res,layer in zip(self.lin,self.layers):
            if self.res:
                f = lambda x: layer(x,key) + res(x)
            else:
                f = lambda x: layer(x,key)
            if len(x.shape)==1:
                x = f(x)
            if len(x.shape)==2:
                x = jax.vmap(f)(x)
            key = jr.split(key)[0]
        return x

class Operator(eqx.Module):
    
    trunk: eqx.Module
    branch: eqx.Module
    func_pe: eqx.Module
    func_space: eqx.Module
    x_dim: int
    tx_dim: int
    u_dim: int
    p_dim: int
    trunk_dims: List[int]
    branch_dims: List[int]
    nonlinear: bool

    def __init__(self, args, module, shared=False): 
        super(Operator, self).__init__()
        self.func_space = getattr(lib.function_spaces, args.func_space)(num_func=args.num_func)
        args, dims, act, _ = get_dim_act(args, module)
        self.x_dim = args.x_dim
        self.tx_dim = 1 + args.x_dim 
        self.u_dim = args.kappa
        self.p_dim = args.p_basis
        self.nonlinear = args.nonlinear

        keys = jr.split(prng(6), 5)
        
        # set dimensions of branch net        
        self.branch_dims = copy.copy(dims)
        self.branch_dims[0] = self.u_dim * args.num_func
        self.branch_dims[-1] = self.p_dim
        self.branch_dims[-1] *= args.x_dim

        # set dimensions of trunk net
        self.trunk_dims = copy.copy(dims)
        #self.trunk_dims[0] = dims[0] - self.u_dim
        
        if module=='pde' and args.nonlinear_pde:
            self.trunk_dims[0] += self.p_dim * args.x_dim 
        elif module=='pde' and not args.nonlinear_pde:
            self.trunk_dims[-1] = self.p_dim
        elif args.nonlinear: 
            self.trunk_dims[0] += self.p_dim * args.x_dim 
        else:
            self.trunk_dims[-1] = self.p_dim
        
        self.branch = eval(args.branch_net)(args=args, in_dim=self.branch_dims[0], out_dim=self.branch_dims[-1]) if not shared else None
        self.trunk = eval(args.trunk_net)(args=args, in_dim=self.trunk_dims[0], out_dim=self.trunk_dims[-1], res=args.trunk_res, norm=args.trunk_norm)
        if args.embed_dims[0] > 0: 
            self.func_pe = eqx.nn.Sequential(
                                [eqx.nn.MLP(args.embed_dims[0], self.branch_dims[0], width_size=4*args.embed_dims[0], depth=2, activation=jax.nn.gelu, key=prng(7)),
                                 eqx.nn.LayerNorm(self.branch_dims[0], use_weight=True, use_bias=True) if args.func_pe_ln else lambda x,key: x
                                ])
        else:
            self.func_pe = lambda x,key: x 

class HGCN(GraphNet):
    
    curvatures: jax.numpy.ndarray 
    
    def __init__(self, args, module):
        super(HGCN, self).__init__(args, module)
        self.manifold = getattr(manifolds, args.manifold)()
        args, dims, act, self.curvatures = get_dim_act(args,module)
        self.curvatures.append(args.c)
        hgc_layers = []
        lin_layers = []
        layer_norms = []
        key = prng(7)
        for i in range(len(dims) - 1):
            c_in, c_out = self.curvatures[i], self.curvatures[i + 1]
            in_dim, out_dim = dims[i], dims[i + 1]
            hgc_layers.append( 
                hyp_layers.HGCNLayer(in_dim, out_dim, key, args, manifold=self.manifold) 
                )
            if args.lin_skip: 
                lin_layers.append(eqx.nn.Linear(in_dim, out_dim, key=key))
            else: 
                lin_layers.append(eqx.nn.Linear(in_dim, out_dim, key=key) if in_dim != out_dim else (lambda x: x) )
            layer_norms.append(eqx.nn.LayerNorm(out_dim))
            key = jax.random.split(key)[0]
        self.layers = nn.Sequential(hgc_layers)
        self.lin = nn.Sequential(lin_layers)
        self.layer_norm = nn.Sequential(layer_norms)
        self.encode_graph = True

    def __call__(self, x, adj, key, w=None):
        return super(HGCN, self).__call__(x, adj, key, w)
