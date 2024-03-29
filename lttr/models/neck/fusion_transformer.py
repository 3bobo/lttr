import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops import rearrange, repeat
from einops.layers.torch import Rearrange

def exists(val):
    return val is not None

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)

class MulPreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn

    def forward(self, query, key, value, **kwargs):
        return self.fn(self.norm(query), self.norm(key), self.norm(value), **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, mult = 4, dropout = 0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * mult),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(dim * mult, dim)
        )

    def forward(self, x):
        return self.net(x)

class Attention(nn.Module):
    def __init__(
        self,
        *,
        dim,
        heads = 8,
        dim_head = 64,
        dropout = 0.
    ):
        super().__init__()
        inner_dim = heads * dim_head
        self.heads =  heads
        self.scale = dim_head ** -0.5

        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        b, n, d, h = *x.shape, self.heads
        q, k, v = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h = h), (q, k, v))

        sim = einsum('b i d, b j d -> b i j', q, k) * self.scale

        attn = sim.softmax(dim = -1)

        out = einsum('b i j, b j d -> b i d', attn, v)

        out = rearrange(out, '(b h) n d -> b n (h d)', h = h)

        return self.to_out(out)

class CrossAttention(nn.Module):
    def __init__(
        self,
        *,
        dim,
        heads = 8,
        dim_head = 64,
        dropout = 0.
    ):
        super().__init__()
        inner_dim = heads * dim_head
        self.heads =  heads
        self.scale = dim_head ** -0.5

        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_k = nn.Linear(dim, inner_dim, bias = False)
        self.to_v = nn.Linear(dim, inner_dim, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, query, key, value):
        b, n, d, h = *query.shape, self.heads
        q = self.to_q(query)
        k = self.to_k(key)
        v = self.to_v(value)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h = h), (q, k, v))

        sim = einsum('b i d, b j d -> b i j', q, k) * self.scale
        # print('sim: ',sim.shape)

        attn = sim.softmax(dim = -1)

        out = einsum('b i j, b j d -> b i d', attn, v)

        out = rearrange(out, '(b h) n d -> b n (h d)', h = h)

        return self.to_out(out)
       
class Encoder(nn.Module):
    def __init__(
        self,
        *,
        image_dim, 
        image_size,
        patch_dim,
        pixel_dim,
        patch_size,
        pixel_size,
        depth,
        out_dim,
        heads = 8,
        dim_head = 64,
        ff_dropout = 0.,
        attn_dropout = 0.
    ):
        super().__init__()
        assert image_size % patch_size == 0, 'image size must be divisible by patch size'

        num_patch_tokens = (image_size // patch_size) ** 2
        pixel_width = patch_size // pixel_size
        num_pixels = pixel_width ** 2

        self.image_dim = image_dim
        self.image_size = image_size
        self.patch_size = patch_size
        self.patch_tokens = nn.Parameter(torch.randn(num_patch_tokens + 1, patch_dim))

        self.to_pixel_tokens = nn.Sequential(
            Rearrange('b c (p1 h) (p2 w) -> (b h w) c p1 p2', p1 = patch_size, p2 = patch_size), # to patch
            nn.Unfold(pixel_width, stride = pixel_width), # to pixel
            Rearrange('... c n -> ... n c'),
            nn.Linear(self.image_dim * pixel_width ** 2, pixel_dim)
        )
        self.patch_pos_emb = nn.Parameter(torch.randn(num_patch_tokens + 1, patch_dim))
        self.pixel_pos_emb = nn.Parameter(torch.randn(num_pixels, pixel_dim))

        layers = nn.ModuleList([])
        for _ in range(depth):
            layers.append(nn.ModuleList([
                PreNorm(pixel_dim, Attention(dim = pixel_dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                PreNorm(pixel_dim, FeedForward(dim = pixel_dim, dropout = ff_dropout)),
                nn.Linear(pixel_dim * num_pixels, patch_dim),
                PreNorm(patch_dim, Attention(dim = patch_dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                PreNorm(patch_dim, FeedForward(dim = patch_dim, dropout = ff_dropout)),
            ]))

        self.layers = layers

        self.mlp_head = nn.Sequential(
            nn.LayerNorm(patch_dim),
            nn.Linear(patch_dim, out_dim)
        )

    def forward(self, x):
        b, _, h, w, patch_size, image_size = *x.shape, self.patch_size, self.image_size
        assert h == image_size and w == image_size, f'height {h} and width {w} of input must be given image size of {image_size}'

        num_patches = image_size // patch_size

        pixels = self.to_pixel_tokens(x)

        patches = repeat(self.patch_tokens, 'n d -> b n d', b = b)

        patches = patches + rearrange(self.patch_pos_emb, 'n d -> () n d')
        
        pixels = pixels + rearrange(self.pixel_pos_emb, 'n d -> () n d')

        
        for pixel_attn, pixel_ff, pixel_to_patch_residual, patch_attn, patch_ff in self.layers:          
            pixels = pixel_attn(pixels) + pixels
            pixels = pixel_ff(pixels) + pixels

            flattened_pixel_tokens = rearrange(pixels, '(b h w) n d -> b (h w) (n d)', h = num_patches, w = num_patches)
            
            patches_residual = pixel_to_patch_residual(flattened_pixel_tokens)

            patches_residual = F.pad(patches_residual, (0, 0, 1, 0), value = 0) # cls token gets residual of 0
            patches = patches + patches_residual

            patches = patch_attn(patches) + patches
            patches = patch_ff(patches) + patches

        out = self.mlp_head(patches)
        patch_pos = self.mlp_head(self.patch_pos_emb)
        return out, patch_pos

class Decoder(nn.Module):
    def __init__(self, dim, dim_head, heads, depth, ff_dropout = 0., attn_dropout = 0):
        super().__init__()
        layers = nn.ModuleList([])
        for _ in range(depth):
            layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim = dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                MulPreNorm(dim, CrossAttention(dim = dim, heads = heads, dim_head = dim_head, dropout = attn_dropout)),
                PreNorm(dim, FeedForward(dim = dim, dropout = ff_dropout)),
            ]))

        self.layers = layers

    def forward(self, sf, tf):
        sf_pe = sf
        tf_pe = tf

        for self_attn, cross_attn, cross_ff in self.layers:          
            sf_pe = self_attn(sf_pe) + sf_pe

            cross_sf = cross_attn(query=sf_pe, key=tf_pe, value=tf_pe) + sf_pe
            cross_sf = cross_ff(cross_sf) + cross_sf
        
        return cross_sf
