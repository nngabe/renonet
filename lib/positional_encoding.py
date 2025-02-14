from typing import Any, Optional
import os
import sys
import time
import numpy as np
import pandas as pd

import torch
from torch import Tensor
from torch_sparse import SparseTensor

from torch_geometric.typing import OptTensor
from torch_geometric.data import Data
from torch_geometric.transforms import BaseTransform
from torch_geometric.utils import (
    get_laplacian,
    to_scipy_sparse_matrix,
)
from torch_geometric.utils.loop import maybe_num_nodes
from torch_geometric.nn import Node2Vec

from torch_geometric.utils import (
    get_laplacian,
    get_self_loop_attr,
    is_torch_sparse_tensor,
    scatter,
    to_edge_index,
    to_scipy_sparse_matrix,
    to_torch_coo_tensor,
    to_torch_csr_tensor,
)


import warnings
warnings.filterwarnings('ignore')

def node2vec(data, dim=128, device='cpu'):
    if hasattr(data,'edge_index'): data = data.edge_index
    model = Node2Vec(data, embedding_dim=dim, walk_length=20,
                     context_size=10, walks_per_node=10,
                     num_negative_samples=1, p=1, q=1, sparse=True).to(device)

    loader = model.loader(batch_size=dim, shuffle=True, num_workers=os.cpu_count())
    optimizer = torch.optim.SparseAdam(list(model.parameters()), lr=0.005)

    def train():
        model.train()
        total_loss = 0
        for pos_rw, neg_rw in loader:
            optimizer.zero_grad()
            loss = model.loss(pos_rw.to(device), neg_rw.to(device))
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        return total_loss / len(loader)

    @torch.no_grad()
    def test():
        model.eval()
        z = model()
        acc = model.test(z[data.train_mask], data.y[data.train_mask],
                         z[data.test_mask], data.y[data.test_mask],
                         max_iter=150)
        return acc

    for epoch in range(21):
        loss = train()
        #acc = test()
        if epoch%10==0: print(f'  Epoch: {epoch:02d}, Loss: {loss:.4f}') 

    @torch.no_grad()
    def plot_points(colors):
        model.eval()
        z = model(torch.arange(data.num_nodes, device=device))
        z = TSNE(n_components=2).fit_transform(z.cpu().numpy())
        y = data.y.cpu().numpy()

        plt.figure(figsize=(8, 8))
        for i in range(2):
            plt.scatter(z[y == i, 0], z[y == i, 1], s=20, color=colors[i])
        plt.axis('off')
        plt.show()

    colors = [
        '#ffc0cb', '#bada55', '#008080', '#420420', '#7fe5f0', '#065535',
        '#ffd700'
    ]
    #plot_points(colors)
    return model

def get_self_loop_attr(edge_index: Tensor, edge_attr: OptTensor = None,
                       num_nodes: Optional[int] = None) -> Tensor:
    r"""Returns the edge features or weights of self-loops
    :math:`(i, i)` of every node :math:`i \in \mathcal{V}` in the
    graph given by :attr:`edge_index`. Edge features of missing self-loops not
    present in :attr:`edge_index` will be filled with zeros. If
    :attr:`edge_attr` is not given, it will be the vector of ones.

    .. note::
        This operation is analogous to getting the diagonal elements of the
        dense adjacency matrix.

    Args:
        edge_index (LongTensor): The edge indices.
        edge_attr (Tensor, optional): Edge weights or multi-dimensional edge
            features. (default: :obj:`None`)
        num_nodes (int, optional): The number of nodes, *i.e.*
            :obj:`max_val + 1` of :attr:`edge_index`. (default: :obj:`None`)

    :rtype: :class:`Tensor`
    """
    loop_mask = edge_index[0] == edge_index[1]
    loop_index = edge_index[0][loop_mask]

    if edge_attr is not None:
        loop_attr = edge_attr[loop_mask]
    else:  # A vector of ones:
        loop_attr = torch.ones_like(loop_index, dtype=torch.float)

    num_nodes = maybe_num_nodes(edge_index, num_nodes)
    full_loop_attr = loop_attr.new_zeros((num_nodes, ) + loop_attr.size()[1:])
    full_loop_attr[loop_index] = loop_attr

    return full_loop_attr

def add_node_attr(data: Data, value: Any,
                  attr_name: Optional[str] = None) -> Data:
    if attr_name is None:
        if 'x' in data:
            x = data.x.view(-1, 1) if data.x.dim() == 1 else data.x
            data.x = torch.cat([x, value.to(x.device, x.dtype)], dim=-1)
        else:
            data.x = value
    else:
        data[attr_name] = value

    return data



class AddLaplacianEigenvectorPE(BaseTransform):
    r"""Adds the Laplacian eigenvector positional encoding from the
    `"Benchmarking Graph Neural Networks" <https://arxiv.org/abs/2003.00982>`_
    paper to the given graph
    (functional name: :obj:`add_laplacian_eigenvector_pe`).

    Args:
        k (int): The number of non-trivial eigenvectors to consider.
        attr_name (str, optional): The attribute name of the data object to add
            positional encodings to. If set to :obj:`None`, will be
            concatenated to :obj:`data.x`.
            (default: :obj:`"laplacian_eigenvector_pe"`)
        is_undirected (bool, optional): If set to :obj:`True`, this transform
            expects undirected graphs as input, and can hence speed up the
            computation of eigenvectors. (default: :obj:`False`)
        **kwargs (optional): Additional arguments of
            :meth:`scipy.sparse.linalg.eigs` (when :attr:`is_undirected` is
            :obj:`False`) or :meth:`scipy.sparse.linalg.eigsh` (when
            :attr:`is_undirected` is :obj:`True`).
    """
    def __init__(
        self,
        k: int,
        attr_name: Optional[str] = 'laplacian_eigenvector_pe',
        is_undirected: bool = False,
        **kwargs,
    ):
        self.k = k
        self.attr_name = attr_name
        self.is_undirected = is_undirected
        self.kwargs = kwargs

    def __call__(self, data: Data) -> Data:
        from scipy.sparse.linalg import eigs, eigsh
        eig_fn = eigs if not self.is_undirected else eigsh

        num_nodes = data.num_nodes
        edge_index, edge_weight = get_laplacian(
            data.edge_index,
            normalization='sym',
            num_nodes=num_nodes,
        )

        L = to_scipy_sparse_matrix(edge_index, edge_weight, num_nodes)

        eig_vals, eig_vecs = eig_fn(
            L,
            k=self.k + 1,
            which='SR' if not self.is_undirected else 'SA',
            return_eigenvectors=True,
            **self.kwargs,
        )

        eig_vecs = np.real(eig_vecs[:, eig_vals.argsort()])
        pe = torch.from_numpy(eig_vecs[:, 1:self.k + 1])
        sign = -1 + 2 * torch.randint(0, 2, (self.k, ))
        pe *= sign

        data = add_node_attr(data, pe, attr_name=self.attr_name)
        return data

#@torch.jit.script
class AddRandomWalkPE(BaseTransform):
    r"""Adds the random walk positional encoding from the `"Graph Neural
    Networks with Learnable Structural and Positional Representations"
    <https://arxiv.org/abs/2110.07875>`_ paper to the given graph
    (functional name: :obj:`add_random_walk_pe`).

    Args:
        walk_length (int): The number of random walk steps.
        attr_name (str, optional): The attribute name of the data object to add
            positional encodings to. If set to :obj:`None`, will be
            concatenated to :obj:`data.x`.
            (default: :obj:`"laplacian_eigenvector_pe"`)
    """
    def __init__(
        self,
        walk_length: int,
        attr_name: Optional[str] = 'random_walk_pe',
    ) -> None:
        self.walk_length = walk_length
        self.attr_name = attr_name

    def forward(self, data: Data) -> Data:
        assert data.edge_index is not None
        row, col = data.edge_index
        N = data.num_nodes
        assert N is not None

        if data.edge_weight is None:
            value = torch.ones(data.num_edges, device=row.device)
        else:
            value = data.edge_weight
        value = scatter(value, row, dim_size=N, reduce='sum').clamp(min=1)[row]
        value = 1.0 / value

        if N <= 2_000:  # Dense code path for faster computation:
            adj = torch.zeros((N, N), device=row.device)
            adj[row, col] = value
            loop_index = torch.arange(N, device=row.device)
        adj = to_torch_csr_tensor(data.edge_index, value, size=data.size())

        def get_pe(out: Tensor) -> Tensor:
            if is_torch_sparse_tensor(out):
                return get_self_loop_attr(*to_edge_index(out), num_nodes=N)
            return out[loop_index, loop_index]

        out = adj
        pe_list = [get_pe(out)]
        for _ in range(self.walk_length - 1):
            out = out @ adj
            pe_list.append(get_pe(out))

        pe = torch.stack(pe_list, dim=-1)
        data = add_node_attr(data, pe, attr_name=self.attr_name)

        return data 


def compute_pos_enc(args, le_size, rw_size, n2v_size, norm, device):
    torch.device(device)
    A = pd.read_parquet(args.adj_path).T.to_numpy()
    adj = A if A.shape[0]==2 else np.where(A)
    edge_index = torch.tensor(adj, device=device)
    data = Data(edge_index=edge_index, device=device)
    print(f' device: {device}')
    print(f' Calculating laplacian PE (dim={le_size})...')
    tic = time.time()
    if le_size>0:
        pe_le = AddLaplacianEigenvectorPE(le_size)
        pe_le = pe_le(data).laplacian_eigenvector_pe.to('cpu').detach().numpy()
    elif le_size==0:
        pe_le = np.array([[] for i in range(A.shape[0])])
    print(f' Done. (time: {time.time()-tic:.1f} s)')
    print(f' Calculating random walk PE (dim={rw_size})...')
    tic = time.time()
    if rw_size>0:
        pe_rw = AddRandomWalkPE(rw_size)
        pe_rw = pe_rw(data.to('cpu')).random_walk_pe.to('cpu').detach().numpy()
        #pe_rw = pe_rw(data).random_walk_pe.to('cpu').detach().numpy()
    elif rw_size==0:
        pe_rw = np.array([[] for i in range(A.shape[0])])
    print(f' Done. (time: {time.time()-tic:.1f} s)')
    print(f' Calculating node2vec PE (dim={n2v_size})...')
    tic = time.time()
    if n2v_size>0:
        pe_n2v = node2vec(data,n2v_size,device)(torch.arange(data.num_nodes,device=device))    
        pe_n2v = pe_n2v.to('cpu').detach().numpy()
    elif n2v_size==0:
        pe_n2v = np.array([[] for i in range(A.shape[0])])
    print(f' Done. (time: {time.time()-tic:.1f} s)')
    pe = [pe_le, pe_n2v]
    norm = lambda x:  (x-x.min())/(x.max()-x.min()) if x.size>0 else x
    pe = [norm(e) for e in pe]
    pe = np.concatenate(pe, axis=-1)
    #pe = np.concatenate([pe_le/pe_le.max(), pe_rw/pe_rw.max(), pe_n2v/pe_n2v.max()], axis=1)

    pe_path = pe_path_from(args)
    pd.DataFrame(pe, columns=np.arange(pe.shape[1]).astype(str)).to_parquet(pe_path)

    return pe

def pe_path_from(args):
    base_path = '/'.join(args.adj_path.split('/')[:-1]) + '/'
    pe_path = base_path + 'pe_' + f'dim{args.pe_size}_' 
    pe_path = pe_path + '_'.join(args.adj_path.split('/')[-1].split('_')[1:])
    return pe_path

def pos_enc(args, le_size=50, rw_size=50, n2v_size=128, norm=False, use_cached=False, device='cpu'):
    """ Read positional encoding from path if it exists else compute from adjacency matrix."""
    pe_path = args.pe_path 
    if use_cached and os.path.exists(pe_path): 
        print(' Reading PE (LapPE, node2vec) from pe_path...', end='')
        pe = pd.read_parquet(pe_path).to_numpy()
        print(' Done.\n')
    else: pe = compute_pos_enc(args, le_size=le_size, rw_size=rw_size, n2v_size=n2v_size, norm=norm, device=device)
    return pe
