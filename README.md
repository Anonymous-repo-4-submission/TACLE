# TACLE

### Repo for paper title: "TACLE: Task and Class-aware Exemplar-free Semi-supervised Class Incremental Learning."

### Setup Environment
```
conda env create -f environment.yml
conda activate tacle
conda install pytorch==1.12.1 torchvision==0.13.1 cudatoolkit=11.6 -c pytorch -c conda-forge
pip install timm==0.5.4
pip install quadprog
pip install POT
```
### run the experiment on CIFAR10
```
bash train_CIFAR10.sh
```
### run the experiment on CIFAR100
```
bash train_CIFAR100.sh
```
