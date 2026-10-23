# Attention Is All You Need (Sample)

## 1. Introduction

The dominant sequence transduction models are based on complex recurrent or convolutional neural networks that include an encoder and a decoder. The best performing models also connect the encoder and decoder through an attention mechanism [1]. Recent work has continued to push the boundaries of recurrent language models and encoder-decoder architectures [2, 3].

Recurrent models typically factor computation along the symbol positions of the input and output sequences. Aligning the positions to steps in computation time, they generate a sequence of hidden states $h_t$, as a function of the previous hidden state $h_{t-1}$ and the input for position $t$. This inherently sequential nature precludes parallelization within training examples.

## 2. Model Architecture

The Transformer follows this overall architecture using stacked self-attention and point-wise, fully connected layers for both the encoder and decoder.

### 2.1 Scaled Dot-Product Attention

We call our particular attention "Scaled Dot-Product Attention". The input consists of queries and keys of dimension $d_k$, and values of dimension $d_v$. We compute the matrix of outputs as:

$$
\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V
$$

The two most commonly used attention functions are additive attention, and dot-product (multiplicative) attention. While for small values of $d_k$ the two mechanisms behave similarly, additive attention outperforms dot product attention without scaling for larger values of $d_k$.

### 2.2 Performance Comparison

The following table summarizes the training cost and BLEU scores across architectures:

| Model Architecture | BLEU Score (EN-DE) | Training FLOPs |
| :--- | :--- | :--- |
| ByteNet [4] | 23.75 | $1.2 \times 10^{19}$ |
| ConvS2S [5] | 25.16 | $9.6 \times 10^{18}$ |
| Transformer (base model) | 27.3 | $3.3 \times 10^{18}$ |
| Transformer (big model) | **28.4** | $2.3 \times 10^{19}$ |

### 2.3 Implementation Snippet

The attention score can be implemented concisely in PyTorch:

```python
import torch
import torch.nn.functional as F

def scaled_dot_product_attention(q, k, v, mask=None):
    d_k = q.size(-1)
    scores = torch.matmul(q, k.transpose(-2, -1)) / (d_k ** 0.5)
    if mask is not None:
        scores = scores.masked_fill(mask == 0, -1e9)
    weights = F.softmax(scores, dim=-1)
    return torch.matmul(weights, v), weights
```

## 3. Conclusion

In this work, we presented the Transformer, the first sequence transduction model based entirely on attention, replacing the recurrent layers most commonly used in encoder-decoder architectures with multi-headed self-attention.
