from torch import nn
from models.TKG_Module import TKG_Module
from utils.utils import filter_none
import torch
from models.DRGCN import DRGCN
from models.RRGCN import RRGCN
import numpy as np
from utils.utils import move_dgl_to_cuda, comp_deg_norm, node_norm_to_edge_norm, cuda
from utils.scores import *
import dgl

import pytorch_lightning as pl
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from utils.dataset import TimeDataset
from collections import OrderedDict
import torch.nn.functional as F
import pdb

class DynamicRGCN(TKG_Module):
    def __init__(self, args, num_ents, num_rels, graph_dict_train, graph_dict_val, graph_dict_test):
        super(DynamicRGCN, self).__init__(args, num_ents, num_rels, graph_dict_train, graph_dict_val, graph_dict_test)
        self.num_layers = self.args.num_layers
        self.train_seq_len = self.args.train_seq_len
        self.test_seq_len = self.args.test_seq_len

        # self.test_seq_len = len(self.total_time)

        self.ent_embeds = nn.Parameter(torch.Tensor(self.num_ents, self.embed_size))
        self.rel_embeds = nn.Parameter(torch.Tensor(self.num_rels * 2, self.embed_size))

        nn.init.xavier_uniform_(self.ent_embeds, gain=nn.init.calculate_gain('relu'))
        nn.init.xavier_uniform_(self.rel_embeds, gain=nn.init.calculate_gain('relu'))

        # self.args.inv_temperature = 1 / self.train_seq_len

    def training_step(self, batch_time, batch_idx):
        if self.args.dataset_dir == 'extrapolation':
            loss = self.forward_single_extra(batch_time)
        else:
            loss = self.forward(batch_time)

        if self.trainer.use_dp or self.trainer.use_ddp2:
            loss = loss.unsqueeze(0)
        tqdm_dict = {'train_loss': loss}
        output = OrderedDict({
            'loss': loss,
            'progress_bar': tqdm_dict,
            'log': tqdm_dict
        })

        self.logger.experiment.log(output)
        # can also return just a scalar instead of a dict (return loss_val)
        return output

    def validation_step(self, batch_time, batch_idx):
        """
        Lightning calls this inside the validation loop
        :param batch:
        :return:
        """

        if self.args.dataset_dir == 'extrapolation':
            ranks, loss = self.evaluate_single_extra(batch_time)
        else:
            ranks, loss = self.evaluate(batch_time)

        # in DP mode (default) make sure if result is scalar, there's another dim in the beginning
        if self.trainer.use_dp or self.trainer.use_ddp2:
            loss = loss.unsqueeze(0)

        log_output = OrderedDict({
            'mean_ranks': ranks.float().mean().item(),
            'val_loss': loss,
        })
        output = OrderedDict({
            'ranks': ranks,
            'val_loss': loss,
        })
        self.logger.experiment.log(log_output)
        return output

    def test_step(self, batch_time, batch_idx):

        if self.args.dataset_dir == 'extrapolation':
            ranks, loss = self.evaluate_single_extra(batch_time, val=False)
        else:
            ranks, loss = self.evaluate(batch_time, val=False)

        # in DP mode (default) make sure if result is scalar, there's another dim in the beginning
        if self.trainer.use_dp or self.trainer.use_ddp2:
            loss = loss.unsqueeze(0)

        log_output = OrderedDict({
            'mean_ranks': ranks.float().mean().item(),
            'test_loss': loss,
        })

        output = OrderedDict({
            'ranks': ranks,
            'test_loss': loss,
        })
        self.logger.experiment.log(log_output)

        return output

    # '''
    def _dataloader(self, times, mode="train"):
        # when using multi-node (ddp) we need to add the  datasampler
        dataset = TimeDataset(times)
        batch_size = self.args.batch_size
        train_sampler = None
        if self.use_ddp:
            train_sampler = DistributedSampler(dataset)

        if mode == 'train':
            should_shuffle = train_sampler is None
        else:
            should_shuffle = False

        loader = DataLoader(
            dataset=dataset,
            batch_size=batch_size if mode == 'train' else 10000,
            shuffle=should_shuffle,
            sampler=train_sampler,
            num_workers=0
        )

        return loader

    @pl.data_loader
    def train_dataloader(self):
        if self.args.dataset_dir == 'extrapolation':
            return self._dataloader(self.train_times)
        else:
            return self._dataloader(self.total_time)

    @pl.data_loader
    def val_dataloader(self):
        if self.args.dataset_dir == 'extrapolation':
            return self._dataloader(self.valid_times)
        else:
            return self._dataloader(self.total_time, 'val')

    @pl.data_loader
    def test_dataloader(self):
        if self.args.dataset_dir == 'extrapolation':
            return self._dataloader(self.valid_times)
        else:
            return self._dataloader(self.total_time, 'val')

    '''
    def evaluate(self, t_list, val=True):
        graph_dict = self.graph_dict_val if val else self.graph_dict_test
        g_train_batched_list, time_list = self.get_batch_graph_list(t_list, self.test_seq_len, self.graph_dict_train)
        g_batched_list, val_time_list = self.get_batch_graph_list(t_list, 1, graph_dict)

        bsz = len(g_train_batched_list[0])
        # print(bsz, 2, self.num_ents, self.embed_size)
        hist_embeddings = self.ent_embeds.new_zeros(bsz, 2, self.num_ents, self.embed_size)

        start_time_tensor = self.ent_embeds.new_zeros(bsz, self.num_ents)

        for t in range(self.test_seq_len - 1):
            g_batched_list_t, node_sizes = self.get_val_vars(g_train_batched_list, t)
            if len(g_batched_list_t) == 0: continue

            first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor = self.get_prev_embeddings(g_batched_list_t, hist_embeddings, start_time_tensor, t)
            first_per_graph_ent_embeds, second_per_graph_ent_embeds = self.get_per_graph_ent_embeds(g_batched_list_t, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds, val=True)
            hist_embeddings = self.update_time_diff_hist_embeddings(first_per_graph_ent_embeds, second_per_graph_ent_embeds, start_time_tensor, g_batched_list_t, t, bsz)

        test_graphs, _ = self.get_val_vars(g_batched_list, -1)
        train_graphs = g_train_batched_list[-1]

        first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor = self.get_prev_embeddings(train_graphs, hist_embeddings, start_time_tensor, self.test_seq_len - 1)
        node_sizes = [len(g.nodes()) for g in train_graphs]
        _, per_graph_ent_embeds = self.get_per_graph_ent_embeds(train_graphs, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds, val=True)
        return self.calc_metrics(per_graph_ent_embeds, test_graphs, time_list[-1], hist_embeddings, start_time_tensor, self.test_seq_len - 1)
    # '''

    def build_model(self):
        if self.args.module == "RRGCN":
            self.ent_encoder = RRGCN(self.args, self.hidden_size, self.embed_size, self.num_rels)
        else:
            self.ent_encoder = DRGCN(self.args, self.hidden_size, self.embed_size, self.num_rels)

    def get_prev_embeddings(self, g_batched_list_t, history_embeddings, start_time_tensor, t):
        first_layer_prev_embeddings = []
        second_layer_prev_embeddings = []
        time_diff_tensor = []
        for i, graph in enumerate(g_batched_list_t):
            node_idx = graph.ndata['id']
            first_layer_prev_embeddings.append(history_embeddings[i][0][node_idx].view(-1, self.embed_size))
            second_layer_prev_embeddings.append(history_embeddings[i][1][node_idx].view(-1, self.embed_size))
            time_diff_tensor.append(t - start_time_tensor[i][node_idx])

        return torch.cat(first_layer_prev_embeddings), torch.cat(second_layer_prev_embeddings), torch.cat(time_diff_tensor)

    def update_time_diff_hist_embeddings(self, first_per_graph_ent_embeds, second_per_graph_ent_embeds, start_time_tensor, g_batched_list_t, time, bsz):
        res = start_time_tensor.new_zeros(bsz, 2, self.num_ents, self.embed_size)
        for i in range(len(first_per_graph_ent_embeds)):
            idx = g_batched_list_t[i].ndata['id'].squeeze()
            res[i][0][idx] = first_per_graph_ent_embeds[i]
            res[i][1][idx] = second_per_graph_ent_embeds[i]
            start_time_tensor[i][idx] = time
        return res

    def get_all_embeds_Gt(self, convoluted_embeds, g, first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor):
        # import pdb;pdb.set_trace()
        all_embeds_g = self.ent_encoder.forward_isolated(self.ent_embeds, first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor.unsqueeze(-1))

        for k, v in g.ids.items():
            all_embeds_g[v] = convoluted_embeds[k]
        return all_embeds_g

    def get_embeds_Gt_extra(self, g, first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor):
        all_embeds_g = self.ent_encoder.forward_isolated(self.ent_embeds, first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor.unsqueeze(-1))
        ent_embed = time_diff_tensor.new_zeros(len(g.ids), self.hidden_size)
        for k, v in g.ids.items():
            ent_embed[k] = all_embeds_g[v]
        # pdb.set_trace()
        return all_embeds_g, ent_embed

    def get_per_graph_ent_embeds(self, g_batched_list_t, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds, val=False):
        if val:
            sampled_graph_list = g_batched_list_t
        else:
            sampled_graph_list = []
            for g in g_batched_list_t:
                src, rel, dst = g.edges()[0], g.edata['type_s'], g.edges()[1]
                half_num_nodes = int(src.shape[0] / 2)
                graph_split_ids = np.random.choice(np.arange(half_num_nodes),
                                                   size=int(0.5 * half_num_nodes), replace=False)
                graph_split_rev_ids = graph_split_ids + half_num_nodes

                sg = g.edge_subgraph(np.concatenate((graph_split_ids, graph_split_rev_ids)), preserve_nodes=True)
                node_norm = comp_deg_norm(sg)
                sg.ndata.update({'id': g.ndata['id'], 'norm': torch.from_numpy(node_norm).view(-1, 1)})
                sg.edata['norm'] = node_norm_to_edge_norm(sg, torch.from_numpy(node_norm).view(-1, 1))
                sg.edata['type_s'] = rel[np.concatenate((graph_split_ids, graph_split_rev_ids))]
                sg.ids = g.ids
                sampled_graph_list.append(sg)

        batched_graph = dgl.batch(sampled_graph_list)
        batched_graph.ndata['h'] = self.ent_embeds[batched_graph.ndata['id']].view(-1, self.embed_size)

        if self.use_cuda:
            move_dgl_to_cuda(batched_graph)
        first_layer_graph, second_layer_graph = self.ent_encoder(batched_graph, first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor)

        first_layer_embeds = first_layer_graph.ndata['h']
        second_layer_embeds = second_layer_graph.ndata['h']
        return first_layer_embeds.split(node_sizes), second_layer_embeds.split(node_sizes)

    def get_val_vars(self, g_batched_list, t):
        g_batched_list_t = filter_none(g_batched_list[t])
        # run RGCN on graph to get encoded ent_embeddings and rel_embeddings in G_t
        node_sizes = [len(g.nodes()) for g in g_batched_list_t]
        return g_batched_list_t, node_sizes

    def evaluate(self, t_list, val=True):
        t_list = torch.tensor([len(self.total_time) - 1])
        self.test_seq_len = len(self.total_time)
        graph_dict = self.graph_dict_val if val else self.graph_dict_test
        g_train_batched_list, time_list = self.get_batch_graph_list(t_list, self.test_seq_len, self.graph_dict_train)
        g_batched_list, val_time_list = self.get_batch_graph_list(t_list, self.test_seq_len, graph_dict)
        bsz = len(g_batched_list[-1])
        # print(bsz, 2, self.num_ents, self.embed_size)
        hist_embeddings = self.ent_embeds.new_zeros(bsz, 2, self.num_ents, self.embed_size)
        start_time_tensor = self.ent_embeds.new_zeros(bsz, self.num_ents)
        all_ranks = []
        all_losses = []
        for t in range(self.test_seq_len):
            print(t, end='\r')
            g_train_batched_list_t, node_sizes = self.get_val_vars(g_train_batched_list, t)
            g_val_batched_list_t, _ = self.get_val_vars(g_batched_list, t)
            # if len(g_train_batched_list_t) == 0: continue
            first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor = self.get_prev_embeddings(g_train_batched_list_t, hist_embeddings, start_time_tensor, t)
            first_per_graph_ent_embeds, second_per_graph_ent_embeds = self.get_per_graph_ent_embeds(g_train_batched_list_t, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds, val=True)

            ranks, losses = self.calc_metrics(second_per_graph_ent_embeds, g_val_batched_list_t, time_list[t], hist_embeddings, start_time_tensor, t)
            hist_embeddings = self.update_time_diff_hist_embeddings(first_per_graph_ent_embeds, second_per_graph_ent_embeds, start_time_tensor, g_train_batched_list_t, t, bsz)
            # import pdb; pdb.set_trace()
            all_ranks.append(ranks)
            all_losses.append(losses)
        return torch.cat(all_ranks), np.mean(all_losses)

    def evaluate_single_extra(self, t_list):
        g_batched_list, time_list = self.get_batch_graph_list(t_list, self.test_seq_len, self.graph_dict_total)
        bsz = len(g_batched_list[0])
        hist_embeddings = self.ent_embeds.new_zeros(bsz, 2, self.num_ents, self.embed_size)
        start_time_tensor = self.ent_embeds.new_zeros(bsz, self.num_ents)

        for t in range(self.test_seq_len - 1):
            g_batched_list_t, node_sizes = self.get_val_vars(g_batched_list, t)
            # import pdb; pdb.set_trace()
            if len(g_batched_list_t) == 0: continue
            first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor = self.get_prev_embeddings(g_batched_list_t, hist_embeddings, start_time_tensor, t)
            first_per_graph_ent_embeds, second_per_graph_ent_embeds = self.get_per_graph_ent_embeds(g_batched_list_t, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds, val=True)
            hist_embeddings = self.update_time_diff_hist_embeddings(first_per_graph_ent_embeds, second_per_graph_ent_embeds, start_time_tensor, g_batched_list_t, t, bsz)
        # import pdb; pdb.set_trace()
        test_graphs, _ = self.get_val_vars(g_batched_list, -1)
        return self.calc_metrics_single_extra(test_graphs, time_list[-1], hist_embeddings, start_time_tensor, self.test_seq_len - 1)

    def pre_forward(self, g_batched_list):
        bsz = len(g_batched_list[0])
        hist_embeddings = self.ent_embeds.new_zeros(bsz, 2, self.num_ents, self.embed_size)
        start_time_tensor = self.ent_embeds.new_zeros(bsz, self.num_ents)
        for t in range(self.train_seq_len - 1):
            g_batched_list_t, node_sizes = self.get_val_vars(g_batched_list, t)
            if len(g_batched_list_t) == 0: continue
            first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor = self.get_prev_embeddings(g_batched_list_t, hist_embeddings, start_time_tensor, t)
            first_per_graph_ent_embeds, second_per_graph_ent_embeds = self.get_per_graph_ent_embeds(g_batched_list_t, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds, val=True)
            hist_embeddings = self.update_time_diff_hist_embeddings(first_per_graph_ent_embeds, second_per_graph_ent_embeds, start_time_tensor, g_batched_list_t, t, bsz)
        return hist_embeddings, start_time_tensor

    def forward_single_extra(self, t_list):
        reconstruct_loss = 0
        g_batched_list, time_batched_list = self.get_batch_graph_list(t_list, self.train_seq_len, self.graph_dict_total)
        hist_embeddings, start_time_tensor = self.pre_forward(g_batched_list)

        train_graphs, time_batched_list_t = g_batched_list[-1], time_batched_list[-1]

        i = 0
        for t, g in zip(time_batched_list_t, train_graphs):
            triplets, neg_tail_samples, neg_head_samples, labels = self.corrupter.single_graph_negative_sampling(t, g, self.num_ents)
            all_embeds_g, ent_embed = self.get_embeds_Gt_extra(g, hist_embeddings[i][0], hist_embeddings[i][1], self.train_seq_len - 1 - start_time_tensor[i])
            loss_tail = self.train_link_prediction(ent_embed, triplets, neg_tail_samples, labels, all_embeds_g, corrupt_tail=True)
            loss_head = self.train_link_prediction(ent_embed, triplets, neg_head_samples, labels, all_embeds_g, corrupt_tail=False)
            reconstruct_loss += loss_tail + loss_head
            i += 1
        return reconstruct_loss

    def forward(self, t_list, reverse=False):
        reconstruct_loss = 0
        g_batched_list, time_batched_list = self.get_batch_graph_list(t_list, self.train_seq_len, self.graph_dict_train)
        hist_embeddings, start_time_tensor = self.pre_forward(g_batched_list)

        train_graphs, time_batched_list_t = g_batched_list[-1], time_batched_list[-1]
        first_prev_graph_embeds, second_prev_graph_embeds, time_diff_tensor = self.get_prev_embeddings(train_graphs, hist_embeddings, start_time_tensor, self.train_seq_len - 1)
        node_sizes = [len(g.nodes()) for g in train_graphs]
        _, per_graph_ent_embeds = self.get_per_graph_ent_embeds(train_graphs, node_sizes, time_diff_tensor, first_prev_graph_embeds, second_prev_graph_embeds)

        i = 0
        for t, g, ent_embed in zip(time_batched_list_t, train_graphs, per_graph_ent_embeds):
            triplets, neg_tail_samples, neg_head_samples, labels = self.corrupter.single_graph_negative_sampling(t, g, self.num_ents)
            all_embeds_g = self.get_all_embeds_Gt(ent_embed, g, hist_embeddings[i][0], hist_embeddings[i][1], self.train_seq_len - 1 - start_time_tensor[i])
            loss_tail = self.train_link_prediction(ent_embed, triplets, neg_tail_samples, labels, all_embeds_g, corrupt_tail=True)
            loss_head = self.train_link_prediction(ent_embed, triplets, neg_head_samples, labels, all_embeds_g, corrupt_tail=False)
            reconstruct_loss += loss_tail + loss_head
            i += 1
        return reconstruct_loss

    '''
    def train_link_prediction_single_extra(self, g, triplets, neg_samples, labels, all_embeds_g, corrupt_tail=True):
        r = self.rel_embeds[triplets[:, 1]]
        if corrupt_tail:
            triple_idx = torch.tensor([g.ids[i] for i in triplets[:, 0].tolist()])
            s = all_embeds_g[triple_idx]
            neg_o = all_embeds_g[neg_samples]
            score = self.calc_score(s, r, neg_o, mode='tail')
        else:
            triple_idx = torch.tensor([g.ids[i] for i in triplets[:, 2].tolist()])
            o = all_embeds_g[triple_idx]
            neg_s = all_embeds_g[neg_samples]
            score = self.calc_score(neg_s, r, o, mode='head')
        predict_loss = F.cross_entropy(score, labels)
        return predict_loss
    '''

    def calc_metrics_single_extra(self, g_list, t_list, hist_embeddings, start_time_tensor, time):
        mrrs, hit_1s, hit_3s, hit_10s, losses = [], [], [], [], []
        ranks = []
        i = 0
        for g, t in zip(g_list, t_list):
            time_diff_tensor = time - start_time_tensor[i]
            all_embeds_g, ent_embed = self.get_embeds_Gt_extra(g, hist_embeddings[i][0], hist_embeddings[i][1], time_diff_tensor)
            index_sample = torch.stack([g.edges()[0], g.edata['type_s'], g.edges()[1]]).transpose(0, 1)
            label = torch.ones(index_sample.shape[0])
            if self.use_cuda:
                index_sample = cuda(index_sample)
                label = cuda(label)
            if index_sample.shape[0] == 0: continue
            rank = self.evaluater.calc_metrics_single_graph(ent_embed, self.rel_embeds, all_embeds_g, index_sample, g, t)
            loss = self.link_classification_loss(ent_embed, self.rel_embeds, index_sample, label)
            ranks.append(rank)
            losses.append(loss.item())
            i += 1
        try:
            ranks = torch.cat(ranks)
        except:
            ranks = cuda(torch.tensor([]).long()) if self.use_cuda else torch.tensor([]).long()

        return ranks, np.mean(losses)

    def calc_metrics(self, g_list, t_list, hist_embeddings, start_time_tensor, time, per_graph_ent_embeds):
        mrrs, hit_1s, hit_3s, hit_10s, losses = [], [], [], [], []
        ranks = []
        i = 0
        for g, t, ent_embed in zip(g_list, t_list, per_graph_ent_embeds):
            time_diff_tensor = time - start_time_tensor[i]
            all_embeds_g = self.get_all_embeds_Gt(ent_embed, g, hist_embeddings[i][0], hist_embeddings[i][1], time_diff_tensor)

            index_sample = torch.stack([g.edges()[0], g.edata['type_s'], g.edges()[1]]).transpose(0, 1)
            label = torch.ones(index_sample.shape[0])
            if self.use_cuda:
                index_sample = cuda(index_sample)
                label = cuda(label)
            if index_sample.shape[0] == 0: continue
            rank = self.evaluater.calc_metrics_single_graph(ent_embed, self.rel_embeds, all_embeds_g, index_sample, g, t)
            loss = self.link_classification_loss(ent_embed, self.rel_embeds, index_sample, label)
            ranks.append(rank)
            losses.append(loss.item())
            i += 1
        try:
            ranks = torch.cat(ranks)
        except:
            ranks = cuda(torch.tensor([]).long()) if self.use_cuda else torch.tensor([]).long()

        return ranks, np.mean(losses)
