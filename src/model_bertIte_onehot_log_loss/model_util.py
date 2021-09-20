import progressbar
import math
import torch 
import heapq
import numpy as np


def get_hit_ratio(rank_list, gt_item):
    for item in rank_list:
        if item == gt_item:
            return 1.0
    return 0

def get_ndcg(rank_list, gt_item):
    for i in range(len(rank_list)):
        item = rank_list[i]
        if item == gt_item:
            return math.log(2) / math.log(i + 2)
    return 0

def evaluate_model_ver1(model, top_k, data, device):
    hits, ndcgs = [], []
    widgets = [progressbar.Percentage(), " ", progressbar.SimpleProgress(), " ", progressbar.Timer()]
    for idx in progressbar.ProgressBar(widgets=widgets)(range(len(data))):
        hr, ndcg = eval_one_rating_ver1(model, idx, top_k, data[idx], device)
        hits.append(hr)
        ndcgs.append(ndcg)
    return np.array(hits).mean(), np.array(ndcgs).mean()

def eval_one_rating_ver1(model, idx, top_k, data, device):
    uid, item_seq, target_ids, attn_mask, labels, items = data["user_ids"], data["items_sequences"],\
                                                                data["target_ids"], data["attn_masks"], data["labels"], data["items"]
    user_ids = torch.tensor(uid, dtype=torch.long).to(device)
    items_sequences = torch.tensor(item_seq, dtype=torch.long).to(device)
    target_ids = torch.tensor(target_ids, dtype=torch.long).to(device)
    attn_masks = torch.tensor(attn_mask, dtype=torch.long).to(device)
    test_item = target_ids[0][-1]
    model.eval()
    click, action = model.forward(user_ids, items_sequences, target_ids, attn_masks)
    # click, action: 1 * 1000
    click, action = click.squeeze(), action.squeeze()
    rating = torch.mul(click, action).detach().cpu().numpy()
    map_score_item = {}
    for i in range(len(user_ids)):
        item = items[i]
        # print(item)
        map_score_item[item] = rating[i]
    rank_list = heapq.nlargest(top_k, map_score_item, key=map_score_item.get)

    hr = get_hit_ratio(rank_list, test_item)
    ndcg = get_ndcg(rank_list, test_item)
    return hr, ndcg

def evaluate_model_ver2(model, top_k, data, device):
    hits, ndcgs = [], []
    widgets = [progressbar.Percentage(), " ", progressbar.SimpleProgress(), " ", progressbar.Timer()]

    for idx in progressbar.ProgressBar(widgets=widgets)(range(len(data))):
        hr, ndcg = eval_one_rating_ver2(model, idx, top_k, data[idx], device)
        hits.append(hr)
        ndcgs.append(ndcg)
    return np.array(hits).mean(), np.array(ndcgs).mean()

def eval_one_rating_ver2(model, idx, top_k, data, device):
    # model = model.to("cpu")
    model.to(device)
    user_ids, items_sequences, target_ids, attn_masks, labels, items = data["user_ids"], data["items_sequences"],\
                                                                data["target_ids"], data["attn_masks"], data["labels"], data["items"]
    user_ids = torch.tensor(user_ids, dtype=torch.long).to(device)
    items_sequences = torch.tensor(items_sequences, dtype=torch.long).to(device)
    target_ids = torch.tensor(target_ids, dtype=torch.long).to(device)
    attn_masks = torch.tensor(attn_masks, dtype=torch.long).to(device)
    test_item = target_ids[-1]
    model.eval()
    click, action = model.forward(user_ids, items_sequences, target_ids, attn_masks)
    rating = torch.mul(click, action).detach().cpu().numpy()
    # print(rating)
    map_score_item = {}
    for i in range(len(user_ids)):
        item = items[i]
        # print(item)
        map_score_item[item] = rating[i]
    rank_list = heapq.nlargest(top_k, map_score_item, key=map_score_item.get)

    hr = get_hit_ratio(rank_list, test_item)
    ndcg = get_ndcg(rank_list, test_item)
    return hr, ndcg

def evaluate_model_ver3(model, top_k, data, device):
    hits, ndcgs = [], []
    reshit, resndcg = [], []
    for tk in top_k:
        hits.append([])
        ndcgs.append([])
    widgets = [progressbar.Percentage(), " ", progressbar.SimpleProgress(), " ", progressbar.Timer()]

    for idx in progressbar.ProgressBar(widgets=widgets)(range(len(data))):
        lst_hr, lst_ndcg = eval_one_rating_ver3(model, idx, top_k, data[idx], device)
        for i, (hr, ndcg) in enumerate(zip(lst_hr, lst_ndcg)):
            hits[i].append(hr)
            ndcgs[i].append(ndcg)
    for lh, ln in zip(hits, ndcgs):
        reshit.append(np.array(lh).mean())
        resndcg.append(np.array(ln).mean())
    return reshit, resndcg

def eval_one_rating_ver3(model, idx, top_k, data, device):
    # model = model.to("cpu")
    model.to(device)
    user_ids, items_sequences, target_ids, attn_masks, labels, items = data["user_ids"], data["items_sequences"],\
                                                                data["target_ids"], data["attn_masks"], data["labels"], data["items"]
    user_ids = torch.tensor(user_ids, dtype=torch.long).to(device)
    items_sequences = torch.tensor(items_sequences, dtype=torch.long).to(device)
    target_ids = torch.tensor(target_ids, dtype=torch.long).to(device)
    attn_masks = torch.tensor(attn_masks, dtype=torch.long).to(device)
    test_item = target_ids[-1]
    model.eval()
    click, action = model.forward(user_ids, items_sequences, target_ids, attn_masks)
    rating = torch.mul(click, action).detach().cpu().numpy()
    # print(rating)
    map_score_item = {}
    for i in range(len(user_ids)):
        item = items[i]
        # print(item)
        map_score_item[item] = rating[i]
    
    list_hr, list_ndcg = [], []
    for tk in top_k:
        rank_list = heapq.nlargest(tk, map_score_item, key=map_score_item.get)

        hr = get_hit_ratio(rank_list, test_item)
        ndcg = get_ndcg(rank_list, test_item)
        list_hr.append(hr)
        list_ndcg.append(ndcg)
    return list_hr, list_ndcg