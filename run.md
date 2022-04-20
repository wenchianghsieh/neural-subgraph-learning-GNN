1. 安装依赖包

```
pip install torch torchvision torchaudio deepsnap==0.1.1 matplotlib networkx numpy scikit_learn scipy seaborn torch torch_geometric test_tube tqdm dgl
```

2. 安装

```
pip install torch-scatter torch-sparse -f https://data.pyg.org/whl/torch-1.11.0+cpu.html
```

4. 训练编码器

```
python -m subgraph_matching.train --node_anchored
```