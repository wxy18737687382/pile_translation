---
license: apache-2.0
---
## UniMERNet: A Universal Network for Mathematical Expression Recognition in Real-World Scenarios. 

Visit our GitHub repository at [UniMERNet](https://github.com/opendatalab/unimernet) for more information. 

## 模型下载

SDK下载
```bash
#安装ModelScope
pip install modelscope
```
```python
#SDK模型下载
from modelscope import snapshot_download
model_dir = snapshot_download('wanderkid/unimernet_base')
```
Git下载
```
#Git模型下载
git clone https://www.modelscope.cn/wanderkid/unimernet_base.git
```



## 引用
```
@misc{wang2024unimernetuniversalnetworkrealworld,
      title={UniMERNet: A Universal Network for Real-World Mathematical Expression Recognition}, 
      author={Bin Wang and Zhuangcheng Gu and Guang Liang and Chao Xu and Bo Zhang and Botian Shi and Conghui He},
      year={2024},
      eprint={2404.15254},
      archivePrefix={arXiv},
      primaryClass={cs.CV},
      url={https://arxiv.org/abs/2404.15254}, 
}

@misc{wang2024cdmreliablemetricfair,
      title={CDM: A Reliable Metric for Fair and Accurate Formula Recognition Evaluation}, 
      author={Bin Wang and Fan Wu and Linke Ouyang and Zhuangcheng Gu and Rui Zhang and Renqiu Xia and Bo Zhang and Conghui He},
      year={2024},
      eprint={2409.03643},
      archivePrefix={arXiv},
      primaryClass={cs.CV},
      url={https://arxiv.org/abs/2409.03643}, 
}

@misc{he2024opendatalabempoweringgeneralartificial,
      title={OpenDataLab: Empowering General Artificial Intelligence with Open Datasets}, 
      author={Conghui He and Wei Li and Zhenjiang Jin and Chao Xu and Bin Wang and Dahua Lin},
      year={2024},
      eprint={2407.13773},
      archivePrefix={arXiv},
      primaryClass={cs.DL},
      url={https://arxiv.org/abs/2407.13773}, 
}
```
```

## MD5 checksums
```
97f4867b4ff4e9a96c8daba8aaa793b4  tokenizer_config.json
351652071425d3d36a634ccc8efb22e8  tokenizer.json
ff4391872dad6688f21ed140009d817b  unimernet_base.pth
```
