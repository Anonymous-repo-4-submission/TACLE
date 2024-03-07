import logging
import numpy as np
import torch
from torch import nn
from torch import optim
from torch.nn import functional as F
from torch.utils.data import DataLoader
from models.base import BaseLearner
from utils.inc_net import FinetuneIncrementalNet
from torchvision import transforms
from torch.distributions.multivariate_normal import MultivariateNormal
import random
from utils.toolkit import tensor2numpy, accuracy
import copy
import os
from utils.nncsl_functions import make_buffer_lst, ClassStratifiedSampler
from collections import defaultdict, Counter
from tqdm import tqdm
from sklearn.utils.class_weight import compute_class_weight
batch_size = 128
split_ratio = 0.1
T = 2
num_workers = 8

class SLCA(BaseLearner): 
    def __init__(self, args):
        super().__init__(args)
        self._network = FinetuneIncrementalNet(args['convnet_type'], pretrained=True) 
        self.log_path = "logs/{}_{}".format(args['model_name'], args['model_postfix'])
        self.model_prefix = args['prefix']
        self.num_classes = args['num_classes']
        
        global epochs, milestones, lrate, lrate_decay, weight_decay, ca_epochs

        if 'bcb_lrscale' in args.keys():
            self.bcb_lrscale = args['bcb_lrscale']
        else:
            self.bcb_lrscale = 1.0/100

        if self.bcb_lrscale == 0:
            self.fix_bcb = True
        else:
            self.fix_bcb = False

        if 'save_before_ca' in args.keys() and args['save_before_ca']:
            self.save_before_ca = True
        else:
            self.save_before_ca = False

        if 'ca_with_logit_norm' in args.keys() and args['ca_with_logit_norm']>0:
            self.logit_norm = args['ca_with_logit_norm']
        else:
            self.logit_norm = None
        
        #global variables
        epochs = args['epochs'] 
        milestones = args['milestones']
        lrate = args['lr']
        lrate_decay = args['lr_decay']
        weight_decay = args['weight_decay']
        ca_epochs = args['ca_epochs'] 

        self.run_id = args['run_id']
        self.seed = args['seed']
        self.task_sizes = []
        self.buffer_lst=None
        self.buffer_size= args['buffer_size']
        self.subset_path= args['subset_path']
        self.subset_path_cls= args['subset_path_cls']
        self.threshold = args["threshold"]
        self.lambda_u = args["lambda_u"]
        self.lambda_s = args["lambda_s"]
        self.mode = args["mode"]
        self.restrictions = args["restrictions"]
        self.taskwise_threshold = args["taskwise_threshold"]
        self.weighted_CE_labeled = args["weighted_CE_labeled"]
        self.weighted_CE_unlabeled = args["weighted_CE_unlabeled"]
        self.strategy = args["strategy"]
        self.increment = args["increment"]
        self.a = args["a"]
        self.b = args["b"]
        self.g = torch.Generator()
        self.g.manual_seed(0)
        self._GLOBAL_SEED = 0
        

    def after_task(self): 
        self._known_classes = self._total_classes
        logging.info('Exemplar size: {}'.format(self.exemplar_size))
        # self.save_checkpoint(self.log_path+'/'+self.model_prefix+'_seed{}'.format(self.seed), head_only=self.fix_bcb)
        self._network.fc.recall() 

    def incremental_train(self, data_manager): 
        self._cur_task += 1  
        task_size = data_manager.get_task_size(self._cur_task) 
        print(f"task_size; {task_size}")
        self.task_sizes.append(task_size) 
        self._total_classes = self._known_classes + data_manager.get_task_size(self._cur_task) 
        self.topk = self._total_classes if self._total_classes<5 else 5
        
        
        self._network.update_fc(data_manager.get_task_size(self._cur_task)) # calls update_fc() in inc_net.py in class FinetuneIncrementalNet
        logging.info('Learning on {}-{}'.format(self._known_classes, self._total_classes))

        self._network.to(self._device)
        self.tasks = list(range(0, (self._cur_task+1) * self.increment))
        
        supervised_dset = data_manager.get_dataset(np.arange(0, self._total_classes), source='train', mode='supervised', tasks =self.tasks, task_idx=self._cur_task, buffer_lst = self.buffer_lst, with_raw=False, keep_file = self.subset_path) #only for the current task
        unsupervised_dset = data_manager.get_dataset(np.arange(0, self._total_classes), source='train', mode='unsupervised', tasks =self.tasks, task_idx=self._cur_task, buffer_lst = self.buffer_lst, with_raw=False, keep_file = self.subset_path, unsupervised= True) #only for the current task
        test_dset = data_manager.get_dataset(np.arange(0, self._total_classes), source='test', mode='test', tasks =self.tasks, task_idx=self._cur_task, buffer_lst = self.buffer_lst, keep_file = self.subset_path)  # All previous classes including current classes
        dset_name = data_manager.dataset_name.lower()
        
        logging.info(f"len_unsupervised data : {len(unsupervised_dset)}")
        logging.info(f"len_supervised data : {len(supervised_dset)}")
        logging.info(f"len_test data : {len(test_dset)}")

        
        self.test_sampler = torch.utils.data.distributed.DistributedSampler(dataset=test_dset, num_replicas=1, rank=0)
        self.supervised_loader = DataLoader(supervised_dset, shuffle=True, batch_size=40, num_workers=num_workers, worker_init_fn=self.seed_worker, generator = self.g)
        self.unsupervised_loader = DataLoader(unsupervised_dset, shuffle=True, batch_size=batch_size, num_workers=num_workers, worker_init_fn=self.seed_worker, generator = self.g)
        self.test_loader = DataLoader(test_dset, sampler=self.test_sampler, batch_size=100, drop_last= False, shuffle=False, num_workers=num_workers, worker_init_fn=self.seed_worker, generator = self.g)
        

        
        # Stage1 training
        self._stage1(task_size)
        if len(self._multiple_gpus) > 1:
            self._network = self._network.module   

        # CA
        self._network.fc.backup() # creates deep copy : function in linear.py
        if self.save_before_ca:
            self.save_checkpoint(self.log_path+'/'+self.model_prefix+'_seed{}_before_ca'.format(self.seed), head_only=self.fix_bcb) # function in base.py
        
        # Compute class mean and covariance
        self._compute_class_mean(data_manager, check_diff=False, oracle=False, unsupervised_dset_loader = self.unsupervised_loader, strategy = self.strategy) 

        # # Stage2 training
        if self._cur_task>-1 and ca_epochs>0: # Runs from second task onwards         #self._cur_task>0 ->  self._cur_task>-1  for making stage 2 training on the Task1 also
            self._stage2_compact_classifier(task_size)
            if len(self._multiple_gpus) > 1:
                self._network = self._network.module
        
    def seed_worker(self, worker_id):
        worker_seed = torch.initial_seed() % 2**32
        np.random.seed(worker_seed)
        random.seed(worker_seed)
    

    def _stage1(self,task_size):
        if self.mode == "fixmatch":
            logging.info("SLCA + fixmatch running ....")
            base_params = self._network.convnet.parameters()  #convnet is the backbone of the model : vit-b-p16
            base_fc_params = [p for p in self._network.fc.parameters() if p.requires_grad==True]
            head_scale = 1. if 'moco' in self.log_path else 1. #Always 1
  
            if not self.fix_bcb:
                base_params = {'params': base_params, 'lr': lrate*self.bcb_lrscale, 'weight_decay': weight_decay}
                base_fc_params = {'params': base_fc_params, 'lr': lrate*head_scale, 'weight_decay': weight_decay}
                network_params = [base_params, base_fc_params]

            else:
                for p in base_params:
                    p.requires_grad = False
                network_params = [{'params': base_fc_params, 'lr': lrate*head_scale, 'weight_decay': weight_decay}]

            optimizer = optim.SGD(network_params, lr=lrate, momentum=0.9, weight_decay=weight_decay)
            scheduler = optim.lr_scheduler.MultiStepLR(optimizer=optimizer, milestones=milestones, gamma=lrate_decay)

            if self.taskwise_threshold:
                logging.info(f"Taskwise Threshold Applied ....  || a: {self.a} || b: {self.b}")
                a = self.a
                b = self.b
                self.threshold = ((1+ torch.exp(torch.tensor(a*self._cur_task)).item())**(-1)) * a +b 
            
            logging.info(f"self.threshold: {self.threshold}")
            
            if self.weighted_CE_unlabeled :
                logging.info(f"Weighted CE loss in UNLABELLED data implementation")
                
            if self.weighted_CE_labeled :
                logging.info(f"Weighted CE loss in LABELLED data implementation")
            
            logging.info(f"Strategy for mean var calculation is {self.strategy}")
            
            labeled_iter = iter(self.supervised_loader)
            # unlabeled_iter = iter(self.unsupervised_loader)
            self._network = self._network.to(self._device)
            if len(self._multiple_gpus) > 1:
                self._network = nn.DataParallel(self._network, self._multiple_gpus)
            

            
            ############################################# weighted classes
            self.y_unlabel_conf_list =[]
            self.y_unlabel_conf_list.append(torch.arange(self.increment))
            
            class_weights_unlabel = torch.ones(self.increment)
        
            ##########################

            for epoch in range(1, epochs+1):

                    losses = 0.
                    LX, LU = 0. , 0.


                    self._network.train()


                    for i, (u_i, inputs_w,inputs_s, _) in enumerate(self.unsupervised_loader):
                        
                    
                    
                        try:
                            _, inputs_x, targets_x = labeled_iter.next()
                        except:
                            labeled_iter = iter(self.supervised_loader)
                            _, inputs_x, targets_x = labeled_iter.next()
                        
                        targets_x = targets_x.to(self._device)

                        inputs = torch.cat((inputs_x, inputs_w, inputs_s)).to(self._device)

                        batch_size = inputs_x.shape[0]
                        # logits = self._network(inputs, bcb_no_grad=self.fix_bcb)['logits'] 
                        
                        logits_feat_list = self._network(inputs, bcb_no_grad=self.fix_bcb)
                        logits = logits_feat_list['logits']
                        features = logits_feat_list['features']
                        logits_x = logits[:batch_size]
                        logits_w, logits_s = logits[batch_size:].chunk(2)
                        # breakpoint()
                        features_w, features_s = features[batch_size:].chunk(2)

                        cur_targets_x = torch.where(targets_x-self._known_classes>=0,targets_x-self._known_classes,-100) 
                        
                        if self.weighted_CE_labeled :
                            Lx = F.cross_entropy(logits_x[:, self._known_classes:], cur_targets_x, weight=class_weights_unlabel.to(self._device).float(), reduction='mean')
                        else :
                            Lx = F.cross_entropy(logits_x[:, self._known_classes:], cur_targets_x, reduction='mean')
                        
                        pseudo_label = torch.softmax(logits_w[:, self._known_classes:], dim= 1)
                        max_prob, hard_label = torch.max(pseudo_label, dim=1)
                        indicator = max_prob>self.threshold
                            
                        self.y_unlabel_conf_list.append(hard_label[indicator].cpu())
                        
                        if epoch < 3: self.lambda_u=0 
                        else: self.lambda_u = 1
                        
                        hard_label = hard_label + self._known_classes

                        if self.restrictions :
                            cur_targets_xu = torch.where(hard_label-self._known_classes>=0,hard_label-self._known_classes,-100) 
                            # Lu = (F.cross_entropy(logits_s[:, self._known_classes:], cur_targets_xu, reduction='none') * indicator).mean()
                            if self.weighted_CE_unlabeled :
                                Lu = (F.cross_entropy(logits_s[:, self._known_classes:], cur_targets_xu, weight=class_weights_unlabel.to(self._device).float(), reduction='none') * indicator).mean() 
                            else:
                                Lu = (F.cross_entropy(logits_s[:, self._known_classes:], cur_targets_xu, reduction='none') * indicator).mean() 
        
                        else: 
                            Lu = (F.cross_entropy(logits_s, hard_label, reduction='none') * indicator * max_prob).mean() 

                        LX += Lx.item()
                        LU += Lu.item()
                        
                        #L_contrastive = (1 - F.cosine_similarity(features_w, features_s, dim=1)).mean()
                        # if self._cur_task > 0:
                        #     loss = self.lambda_s * Lx + self.lambda_u * Lu #+ self.lambda_u * torch.sum(torch.softmax(logits_w[:, :self._known_classes]/self.T, dim= 1))
                        # else:   
                        loss = self.lambda_s * Lx + self.lambda_u * Lu #+ L_contrastive

                        optimizer.zero_grad()
                        loss.backward()
                        optimizer.step()
                        losses += loss.item()
                        
                        y_unlabel_conf = torch.hstack(self.y_unlabel_conf_list)
                        # print('check y_unlabel_conf', torch.unique(y_unlabel_conf))
                        class_weights_unlabel = compute_class_weight(class_weight="balanced", classes=np.arange(self.increment), y=y_unlabel_conf.cpu().numpy())
                        class_weights_unlabel = torch.tensor(class_weights_unlabel)
                        class_weights_unlabel = (class_weights_unlabel/torch.max(class_weights_unlabel)) + 1
                        
                        # if epoch%10 == 0:
                    us_acc, us_acc_with_thresh, samples_involved, targets_u_counts = self._compute_accuracy(self._network, self.unsupervised_loader, unsupervised = True)
                    logging.info(f"TASK_IDX: {self._cur_task} || EPOCH: {epoch} || loss: {losses:.4f}|| Test_acc: {self._compute_accuracy(self._network, self.test_loader)} || us_acc: {us_acc} || us_acc_with_thresh: {us_acc_with_thresh} || samples_involved: {samples_involved}")
                    logging.info(f"dis: {targets_u_counts}")
                    # logging.info(f"cls_thresholds: {cls_thresholds}")
                    # logging.info(f"self.tasks: {self.tasks}")
                    self.classwise_acc(self._network, self.test_loader)
                    logging.info(f"class_weights_unlabel: {class_weights_unlabel}")
                    
                    self.y_unlabel_conf_list =[]
                    self.y_unlabel_conf_list.append(torch.arange(self.increment))

                    filtered_targets_u_counts = {key: value for key, value in targets_u_counts.items() if key >= self._known_classes}

                    class_labels = torch.tensor([label - self._known_classes for label, count in filtered_targets_u_counts.items() for _ in range(count)])
                    self.y_unlabel_conf_list.append(class_labels)
                    
                    
                    scheduler.step()
                    
        elif self.mode == "slca" :
            
            logging.info("Original SLCA running......")
            # breakpoint()
            base_params = self._network.convnet.parameters()  #convnet is the backbone of the model : vit-b-p16
            base_fc_params = [p for p in self._network.fc.parameters() if p.requires_grad==True]
            head_scale = 1. if 'moco' in self.log_path else 1. #Always 1

            
            
            if not self.fix_bcb:
                base_params = {'params': base_params, 'lr': lrate*self.bcb_lrscale, 'weight_decay': weight_decay}
                base_fc_params = {'params': base_fc_params, 'lr': lrate*head_scale, 'weight_decay': weight_decay}
                network_params = [base_params, base_fc_params]

            else:
                for p in base_params:
                    p.requires_grad = False
                network_params = [{'params': base_fc_params, 'lr': lrate*head_scale, 'weight_decay': weight_decay}]

            optimizer = optim.SGD(network_params, lr=lrate, momentum=0.9, weight_decay=weight_decay)
            scheduler = optim.lr_scheduler.MultiStepLR(optimizer=optimizer, milestones=milestones, gamma=lrate_decay)

            labeled_iter = iter(self.supervised_loader)
            
            self._network = self._network.to(self._device)
            if len(self._multiple_gpus) > 1:
                self._network = nn.DataParallel(self._network, self._multiple_gpus)
            
            # learning_status = [-1] * self.N
            # cls_thresholds = torch.ones(task_size, device=self._device) * 0.65 # last is out of task prediction

            for epoch in range(1, epochs+1):
                    losses = 0.

                    self._network.train()

                    for i, (x_i, inputs_x, targets_x) in enumerate(self.supervised_loader):
                        
                        targets_x = targets_x.to(self._device)
                        logits_x = self._network(inputs_x, bcb_no_grad=self.fix_bcb)['logits'] 

                        cur_targets_x = torch.where(targets_x-self._known_classes>=0,targets_x-self._known_classes,-100) 

                        loss = F.cross_entropy(logits_x[:, self._known_classes:], cur_targets_x, reduction='mean')
                        
                        optimizer.zero_grad()
                        loss.backward()
                        optimizer.step()

                        losses += loss.item()
                        
                    # us_acc, us_acc_with_thresh, samples_involved, targets_u_counts = self._compute_accuracy(self._network, self.unsupervised_loader, unsupervised = True)
                    logging.info(f"TASK_IDX: {self._cur_task} || EPOCH: {epoch} || loss: {losses:.4f}|| Test_acc: {self._compute_accuracy(self._network, self.test_loader)}")
                    # logging.info(f"dis: {targets_u_counts}")
                    # logging.info(f"cls_thresholds: {cls_thresholds}")
                    # logging.info(f"self.tasks: {self.tasks}")
                    self.classwise_acc(self._network, self.test_loader)
                    scheduler.step()
                    
        else:
            print("Approach is not implemented")
            print(c)
            
    def _stage2_compact_classifier(self, task_size): # Called after first task # task_size = 10

        for p in self._network.fc.parameters():
            p.requires_grad=True
            
        run_epochs = ca_epochs            #5
        crct_num = self._total_classes     
        param_list = [p for p in self._network.fc.parameters() if p.requires_grad]
        network_params = [{'params': param_list, 'lr': lrate,
                           'weight_decay': weight_decay}]
        optimizer = optim.SGD(network_params, lr=lrate, momentum=0.9, weight_decay=weight_decay)
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer=optimizer, T_max=run_epochs)

        self._network.to(self._device)
        if len(self._multiple_gpus) > 1:
            self._network = nn.DataParallel(self._network, self._multiple_gpus)

        self._network.eval() # Only dropout and batchnorm are affected
        for epoch in range(run_epochs): #5
            losses = 0.

            sampled_data = []
            sampled_label = []
            num_sampled_pcls = 256
        
            for c_id in range(crct_num):
                t_id = c_id//task_size
                decay = (t_id+1)/(self._cur_task+1)*0.1 
                cls_mean = torch.tensor(self._class_means[c_id], dtype=torch.float64).to(self._device)*(0.9+decay) # torch.from_numpy(self._class_means[c_id]).to(self._device)
                cls_cov = self._class_covs[c_id].to(self._device)
                
                m = MultivariateNormal(cls_mean.float(), cls_cov.float())

                sampled_data_single = m.sample(sample_shape=(num_sampled_pcls,)) #[256, 768]
                sampled_data.append(sampled_data_single)                
                sampled_label.extend([c_id]*num_sampled_pcls)

            sampled_data = torch.cat(sampled_data, dim=0).float().to(self._device) #[5120, 768]
            sampled_label = torch.tensor(sampled_label).long().to(self._device) #[5120]

            inputs = sampled_data
            targets= sampled_label

            sf_indexes = torch.randperm(inputs.size(0))
            inputs = inputs[sf_indexes]
            targets = targets[sf_indexes]
            
            for _iter in range(crct_num): #20
                inp = inputs[_iter*num_sampled_pcls:(_iter+1)*num_sampled_pcls]
                tgt = targets[_iter*num_sampled_pcls:(_iter+1)*num_sampled_pcls] 
                outputs = self._network(inp, bcb_no_grad=True, fc_only=True)
                logits = outputs['logits'] #[256, 20]

                if self.logit_norm is not None: #0.1
                    per_task_norm = []
                    prev_t_size = 0
                    cur_t_size = 0

                    for _ti in range(self._cur_task+1): #1+1 ->
                        cur_t_size += self.task_sizes[_ti]
                        temp_norm = torch.norm(logits[:, prev_t_size:cur_t_size], p=2, dim=-1, keepdim=True) + 1e-7 #[256, 1] : Calculate norm for each task
                        per_task_norm.append(temp_norm)
                        prev_t_size += self.task_sizes[_ti]
                    per_task_norm = torch.cat(per_task_norm, dim=-1) #[256, 2] ->
                    norms = per_task_norm.mean(dim=-1, keepdim=True) #[256, 1] -> Calculate mean of norms for per_task_norm along task dimension
                        
                    norms_all = torch.norm(logits[:, :crct_num], p=2, dim=-1, keepdim=True) + 1e-7 #Calculate norm for all classes ; [256, 1]
                    decoupled_logits = torch.div(logits[:, :crct_num], norms) / self.logit_norm #[256, 20]
                    loss = F.cross_entropy(decoupled_logits, tgt)

                else:
                    loss = F.cross_entropy(logits[:, :crct_num], tgt)

                optimizer.zero_grad()
                loss.backward() #Backpropagation for fc layers only
                optimizer.step()
                losses += loss.item()

            scheduler.step()
            us_acc, us_acc_with_thresh, samples_involved, targets_u_counts = self._compute_accuracy(self._network, self.unsupervised_loader, unsupervised = True)
            test_acc = self._compute_accuracy(self._network, self.test_loader)
            info = 'CA Task {} => Loss {:.3f}, Test_accy {:.3f}, US_accy {:.3f}, us_acc_with_thresh {:.3f}, samples_involved {:.3f}'.format(self._cur_task, losses/self._total_classes, test_acc, us_acc, us_acc_with_thresh, samples_involved) 
            logging.info(info)
            logging.info(f"dis: {targets_u_counts}")

    def count_unique_elements(self, tensor):
        unique_elements, counts = torch.unique(tensor, return_counts=True)
        result_dict = dict(zip(unique_elements.tolist(), counts.tolist()))
        return result_dict
    
    def classwise_acc(self, model, loader):
        num_classes = len(self.tasks)
        # logging.info(f"classes_list: {self.tasks} || num_classes; {num_classes}")
        # breakpoint()
        acc = []
        total = [0. for i in range(num_classes)]
        correct = [0. for i in range(num_classes)]
        ip = 0. 
        for i, (_, inputs, targets) in enumerate(loader):
            inputs = inputs.to(self._device)
            with torch.no_grad():
                outputs = model(inputs)['logits']
            predicts = torch.max(outputs, dim=1)[1]
            ip += len(inputs)
            for c in self.tasks:
                idx = torch.where(targets==c)

                correct[c] += (predicts[idx]==c).sum()
                total[c] += len(idx[0])
            # breakpoint()
        for c,t in zip(correct, total):
            acc.append((c/t).item())
            # print(f"correct: {ip}")
            # print(f"total: {sum(total)}")
            # print(f"correct: {sum(correct)}")

        logging.info(f"acc: {acc}")

            # correct += (predicts.cpu() == targets).sum()
            # total += len(targets)
