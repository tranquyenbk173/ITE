import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from tqdm.notebook import tqdm
from terminaltables import AsciiTable
import progressbar
import numpy as np
import math
import time
import csv
import os

import config
from model_util import *
from data_util import Data_Utils

class CausalSelfAttention(nn.Module):
    """
    A vanilla multi-head masked self-attention layer with a projection at the end.
    It is possible to use torch.nn.MultiheadAttention here but I am including an
    explicit implementation here to show that there is nothing too scary here.
    """

    def __init__(self, config):
        super().__init__()
        assert config.n_embd % config.n_head == 0
        # key, query, value projections for all heads
        self.key = nn.Linear(config.n_embd, config.n_embd) 
        self.query = nn.Linear(config.n_embd, config.n_embd) 
        self.value = nn.Linear(config.n_embd, config.n_embd) 
        # regularization
        self.attn_drop = nn.Dropout(config.attn_pdrop)
        self.resid_drop = nn.Dropout(config.resid_pdrop)
        # output projection
        self.proj = nn.Linear(config.n_embd, config.n_embd)
        # causal mask to ensure that attention is only applied to the left in the input sequence
        self.n_head = config.n_head
        self.device = config.device

    def forward(self, x, attn_mask1, layer_past=None):
        B, T, C = x.size() # 40 * 10 * 2f
        
        attn_mask = attn_mask1.view(B, 1, T).detach().cpu().numpy()
        attn_mask = np.tile(attn_mask, [1, T, 1])
        attn_mask = np.expand_dims(attn_mask, axis=1)
        attn_mask = torch.tensor(attn_mask).long().to(self.device)
        
        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        k = self.key(x).view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        q = self.query(x).view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        v = self.value(x).view(B, T, self.n_head, C // self.n_head).transpose(1, 2) # (B, nh, T, hs)
        
        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = (q @ k.transpose(-2, -1)) * (1.0 / math.sqrt(k.size(-1)))
        
        att = att.masked_fill(attn_mask[:,:,:T,:T] == 0, float('-inf'))
        
        att = F.softmax(att, dim=-1)
        att = self.attn_drop(att)
        y = att @ v # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head outputs side by side
        
        # output projection
        y = self.resid_drop(self.proj(y))
        return y

class Block(nn.Module):
    """ an unassuming Transformer block """

    def __init__(self, config):
        super().__init__()
        self.ln1 = nn.LayerNorm(config.n_embd)
        self.ln2 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config)
        self.mlp = nn.Sequential(
            nn.Linear(config.n_embd, 4 * config.n_embd),
            nn.GELU(),
            nn.Linear(4 * config.n_embd, config.n_embd),
            nn.Dropout(config.resid_pdrop),
        )

    def forward(self, x, attn_mask):
        x = x + self.attn(self.ln1(x), attn_mask)
        x = x + self.mlp(self.ln2(x))
        return x

class BertIte(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.cf = config
        
        self.num_user = self.cf.num_user
        self.num_item = self.cf.num_item
        self.num_factor_gmf = self.cf.num_factor
        self.hidden_explicit = self.cf.num_factor * 2

        self.embedding_user = nn.Embedding(self.num_user, self.num_factor_gmf) # 40 * 10 * 2f
        self.embedding_item = nn.Embedding(self.num_item + 1, self.num_factor_gmf) # 40 * 10 * 2f

        self.blocks = nn.ModuleList([Block(self.cf) for _ in range(self.cf.n_layer)])

        self.implicit = nn.Linear(self.num_factor_gmf, 1)

        self.explicit1 = nn.Linear(self.num_factor_gmf, self.hidden_explicit)
        self.explicit = nn.Linear(self.hidden_explicit, 1)
        self.loss_fn = nn.BCELoss()
        self.act_fn = nn.Sigmoid()
        self.eta = self.cf.eta
        self.reg_lambda = self.cf.reg_lambda
        self.device = self.cf.device


    def forward(self, uids, item_sequences, target_items, attention_masks):
        self.uids_embedding = self.embedding_user(uids) 
        self.uids_embedding = torch.unsqueeze(self.uids_embedding, dim=1)

        self.item_sequences_embedding = self.embedding_item(item_sequences)

        self.target_items_embedding = self.embedding_item(target_items)
        input_bert = torch.cat((self.uids_embedding, self.item_sequences_embedding), dim=1)

        for block in self.blocks:
            input_bert = block.forward(input_bert, attention_masks)
        
        user_representation = input_bert[:, 0, :]
        user_target_representation = torch.mul(user_representation, self.target_items_embedding)

        click = self.act_fn(self.implicit(user_target_representation))
        click = click.squeeze()

        x = self.act_fn(self.explicit1(user_target_representation))
        action = self.act_fn(self.explicit(x))
        action = action.squeeze()
        return click, action
    
    def compute_loss(self, click, action, implicit_labels, explicit_labels):
        im_loss = self.loss_fn(click.type(torch.FloatTensor), implicit_labels.type(torch.FloatTensor))
        im_loss = torch.mean(im_loss)

        ex_loss = self.loss_fn(action.type(torch.FloatTensor), explicit_labels.type(torch.FloatTensor))
        ex_loss = torch.mean(ex_loss)
        regularizer = torch.add(torch.mean(torch.square(self.uids_embedding)), torch.mean(torch.square(self.item_sequences_embedding)))

        return self.eta * im_loss + ex_loss + self.reg_lambda * regularizer


class Manager:
    
    def __init__(self, root_path, params, log_path, saved_model_path, restore=True, save_log=True, save_model=True):
        super().__init__()
        self.root_path = root_path
        self.train_path = root_path + 'without_implicit_in_train/ratings_train.txt'
        self.log_path = log_path
        self.saved_model_path = saved_model_path
        self.restore = restore
        
        self.cf = config.Config(self.root_path, params)

        
    def train_and_evaluate(self):
        
        #Prepare data and model:
        du = Data_Utils(self.train_path, self.root_path, self.cf)
        test_data = du.load_test_data_s_ite(self.root_path + "ratings_test.txt")
        negative_data = du.load_negative_data(self.root_path + "_explicit.test.negative")
        data4test = du.preprocess_test(test_data, negative_data)
        
        model = BertIte(self.cf)
        model = model.to(self.cf.device)
        optimizer = optim.Adam(model.parameters(), lr = self.cf.lr)
    
        ckp_path = self.saved_model_path + f'{self.cf.num_factor}_{self.cf.batch_size}_{self.cf.eta}_{self.cf.lr}'
        log_path = self.log_path + f'{self.cf.num_factor}_{self.cf.batch_size}_{self.cf.eta}_{self.cf.lr}'
        epoch_ii=-1
        
        #Load checkpoint or not?
        if self.restore:
            checkpoint = torch.load(ckp_path)
            model.load_state_dict(checkpoint['model_state_dict'])
            epoch_ii = checkpoint['epoch']
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            log_path = log_path + f'({str(epoch_ii)})'
            print('===>>>>>>>>>>>>>>>>>>>Load checkpoint: ', epoch_ii)
    
        epochs = 200

        #Evaluating at initialization
        print('Evaluating at initialization......')
        eval_result_data = [["Epoch", "Train_loss", "Top k", "Hit", "NDCG"]]
        top_k = [5, 10, 20, 30, 40, 50]
        lst_ex_hit, lst_ex_ndcg = evaluate_model_ver3(model,
                                            top_k=top_k,
                                            data=data4test,
                                            device=self.cf.device)
        for tk, exh, exn in zip(top_k, lst_ex_hit, lst_ex_ndcg):
            print(f"Ex_hit {tk}: {exh}")
            print(f"Ex_ndcg {tk}: {exn}")
            eval_result_data.append([0, 0, tk, exh, exn])
            
            with open(log_path,"w") as log:
                log.write(AsciiTable(eval_result_data).table)
        print(AsciiTable(eval_result_data).table)
        
        #Training...
        for epoch_i in range(epoch_ii+1, epochs):
    
            users_ids, items_seq, targets_ids, implicit_labels, explicit_labels, attention_masks = du.preprocess_data()
            users_ids_tensor = torch.tensor(users_ids, dtype=torch.long)
            items_seq_tensor = torch.tensor(items_seq, dtype=torch.long)
            targets_ids_tensor = torch.tensor(targets_ids, dtype=torch.long)
            implicit_labels_tensor = torch.tensor(implicit_labels, dtype=torch.long)
            explicit_labels_tensor = torch.tensor(explicit_labels, dtype=torch.long)
            attention_masks_tensor = torch.tensor(attention_masks, dtype=torch.long)
            
            train_data = TensorDataset(users_ids_tensor, items_seq_tensor, targets_ids_tensor,\
                                       implicit_labels_tensor, explicit_labels_tensor, attention_masks_tensor)
            train_sampler = SequentialSampler(train_data)
            train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=self.cf.batch_size)
            n = len(train_data) // self.cf.batch_size
            
            print("======= Epoch {:} / {:} ===================================".format(epoch_i + 1, epochs))
            print("Training .... ")
    
            total_loss = 0
            model.train()
            total_loss = 0
    
            step = 0
            widgets = [progressbar.Percentage(), " ", progressbar.SimpleProgress(), " ", progressbar.Timer()]
            for batch in progressbar.ProgressBar(widgets=widgets)(train_dataloader):
                step += 1
                uids = batch[0].to(self.cf.device)
                item_sequences = batch[1].to(self.cf.device)
                target_ids = batch[2].to(self.cf.device)
                im_labels = batch[3].to(self.cf.device)
                ex_labels = batch[4].to(self.cf.device)
                attn_masks = batch[5].to(self.cf.device)
    
                model.zero_grad()
                t = time.time()
                click, action = model.forward(
                    uids,
                    item_sequences,
                    target_ids,
                    attn_masks,
                )
    
                loss = model.compute_loss(click, action, im_labels, ex_labels)
    
                loss.backward()
                optimizer.step()
                total_loss += loss.item()
                
            print("Loss epoch %d : %f" % (epoch_i + 1, total_loss/n))
            if (epoch_i+1) % 10 == 0:

                torch.save({
                    'epoch': epoch_i,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "loss": total_loss
                }, ckp_path)
                print("Evaluate model epoch %d ..." % (epoch_i + 1))
    
                top_k = [5, 10, 20, 30, 40, 50]
                lst_ex_hit, lst_ex_ndcg = evaluate_model_ver3(model,
                                                    top_k=top_k,
                                                    data=data4test,
                                                    device=self.cf.device)
                for tk, exh, exn in zip(top_k, lst_ex_hit, lst_ex_ndcg):
                    print(f"Ex_hit {tk}: {exh}")
                    print(f"Ex_ndcg {tk}: {exn}")
                    eval_result_data.append([str(epoch_i + 1), total_loss / n, tk, exh, exn])
                    
                    with open(log_path,"w") as log:
                        log.write(AsciiTable(eval_result_data).table)
                        
                print(AsciiTable(eval_result_data).table)
                        