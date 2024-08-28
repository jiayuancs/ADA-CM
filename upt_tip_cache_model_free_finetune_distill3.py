"""
Unary-pairwise transformer for human-object interaction detection

Fred Zhang <frederic.zhang@anu.edu.au>

The Australian National University
Australian Centre for Robotic Vision
"""


from builtins import Exception
import os
import torch
import torch.distributed as dist


from torch import nn, Tensor
from typing import Optional, List
from torchvision.ops.boxes import batched_nms, box_iou

from ops import binary_focal_loss_with_logits

import sys
from hico_list import hico_verb_object_list,hico_verbs,hico_verbs_sentence,hico_verbs_sentence_2
from vcoco_list import vcoco_verbs_sentence
sys.path.append('detr')
# print(sys.path)
from detr.models import build_model
# from d_detr.models import build_model as build_model_d_detr
from util import box_ops
from util.misc import nested_tensor_from_tensor_list
import pdb
import CLIP_models_adapter_prior2
import torchvision
from collections import OrderedDict
import numpy as np
import torch.nn.functional as F
from transformer_module import TransformerDecoderLayer
from CLIP.clip.simple_tokenizer import SimpleTokenizer as _Tokenizer
import clip 
from ops import box_xyxy_to_cxcywh, box_cxcywh_to_xyxy
import pickle, random
from tqdm import tqdm
from hico_text_label import hico_unseen_index
import hico_text_label

_tokenizer = _Tokenizer()
class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x

class Weight_Pred(nn.Module):
    def __init__(self, input_dim, output_dim) -> None:
        super().__init__()
        self.linear1 = MLP(input_dim=input_dim, hidden_dim=512, output_dim=128, num_layers=2)
        self.drop1 = nn.Dropout()
        self.linear2 = MLP(input_dim=128, hidden_dim=32, output_dim=3, num_layers=2)
    
    def forward(self, x):
        x = self.drop1(self.linear1(x))
        x = self.linear2(x)
        return F.sigmoid(x)

class TextEncoder(nn.Module):
    def __init__(self, clip_model):
        super().__init__()
        self.transformer = clip_model.transformer
        self.positional_embedding = clip_model.positional_embedding
        self.ln_final = clip_model.ln_final
        self.text_projection = clip_model.text_projection
        self.dtype = clip_model.dtype

    def forward(self, prompts, tokenized_prompts):
        x = prompts + self.positional_embedding.type(self.dtype)
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_final(x).type(self.dtype)

        # x.shape = [batch_size, n_ctx, transformer.width]
        # take features from the eot embedding (eot_token is the highest number in each sequence)
        x = x[torch.arange(x.shape[0]), tokenized_prompts.argmax(dim=-1)] @ self.text_projection

        return x

class PromptLearner(nn.Module):
    def __init__(self, args, classnames, clip_model):
        super().__init__()
        n_cls = len(classnames)
        n_ctx = args.N_CTX # cfg.TRAINER.COOP.N_CTX
        ctx_init = args.CTX_INIT ## cfg.TRAINER.COOP.CTX_INIT
        dtype = clip_model.dtype
        ctx_dim = clip_model.ln_final.weight.shape[0]
        # clip_imsize = clip_model.visual.input_resolution
        # cfg_imsize = cfg.INPUT.SIZE[0]
        # assert cfg_imsize == clip_imsize, f"cfg_imsize ({cfg_imsize}) must equal to clip_imsize ({clip_imsize})"

        if ctx_init:
            # use given words to initialize context vectors
            ctx_init = ctx_init.replace("_", " ")
            n_ctx = len(ctx_init.split(" "))
            prompt = clip.tokenize(ctx_init)
            with torch.no_grad():
                embedding = clip_model.token_embedding(prompt).type(dtype)
            ctx_vectors = embedding[0, 1 : 1 + n_ctx, :]
            prompt_prefix = ctx_init
        else:
            # random initialization
            if args.CSC: # cfg.TRAINER.COOP.CSC:
                print("Initializing class-specific contexts")
                ctx_vectors = torch.empty(n_cls, n_ctx, ctx_dim, dtype=dtype)
            else:
                print("Initializing a generic context")
                ctx_vectors = torch.empty(n_ctx, ctx_dim, dtype=dtype)
            nn.init.normal_(ctx_vectors, std=0.02)
            prompt_prefix = " ".join(["X"] * n_ctx)

        print(f'Initial context: "{prompt_prefix}"')
        print(f"Number of context words (tokens): {n_ctx}")

        self.ctx = nn.Parameter(ctx_vectors)  # to be optimized
        
        # classnames = [name.replace("_", " ") for name in classnames]
        name_lens = [len(_tokenizer.encode(name)) for name in classnames]
        prompts = [prompt_prefix + " " + name + "." for name in classnames]

        tokenized_prompts = torch.cat([clip.tokenize(p) for p in prompts])
        with torch.no_grad():
            embedding = clip_model.token_embedding(tokenized_prompts).type(dtype)

        # These token vectors will be saved when in save_model(),
        # but they should be ignored in load_model() as we want to use
        # those computed using the current class names
        self.register_buffer("token_prefix", embedding[:, :1, :])  # SOS
        self.register_buffer("token_suffix", embedding[:, 1 + n_ctx :, :])  # CLS, EOS

        self.n_cls = n_cls
        self.n_ctx = n_ctx
        self.tokenized_prompts = tokenized_prompts  # torch.Tensor
        self.name_lens = name_lens
        self.class_token_position = args.CLASS_TOKEN_POSITION  # cfg.TRAINER.COOP.CLASS_TOKEN_POSITION

    def forward(self):
        ctx = self.ctx
        if ctx.dim() == 2:
            ctx = ctx.unsqueeze(0).expand(self.n_cls, -1, -1)

        prefix = self.token_prefix
        suffix = self.token_suffix

        if self.class_token_position == "end":
            prompts = torch.cat(
                [
                    prefix,  # (n_cls, 1, dim)
                    ctx,     # (n_cls, n_ctx, dim)
                    suffix,  # (n_cls, *, dim)
                ],
                dim=1,
            )

        elif self.class_token_position == "middle":
            half_n_ctx = self.n_ctx // 2
            prompts = []
            for i in range(self.n_cls):
                name_len = self.name_lens[i]
                prefix_i = prefix[i : i + 1, :, :]
                class_i = suffix[i : i + 1, :name_len, :]
                suffix_i = suffix[i : i + 1, name_len:, :]
                ctx_i_half1 = ctx[i : i + 1, :half_n_ctx, :]
                ctx_i_half2 = ctx[i : i + 1, half_n_ctx:, :]
                prompt = torch.cat(
                    [
                        prefix_i,     # (1, 1, dim)
                        ctx_i_half1,  # (1, n_ctx//2, dim)
                        class_i,      # (1, name_len, dim)
                        ctx_i_half2,  # (1, n_ctx//2, dim)
                        suffix_i,     # (1, *, dim)
                    ],
                    dim=1,
                )
                prompts.append(prompt)
            prompts = torch.cat(prompts, dim=0)

        elif self.class_token_position == "front":
            prompts = []
            for i in range(self.n_cls):
                name_len = self.name_lens[i]
                prefix_i = prefix[i : i + 1, :, :]
                class_i = suffix[i : i + 1, :name_len, :]
                suffix_i = suffix[i : i + 1, name_len:, :]
                ctx_i = ctx[i : i + 1, :, :]
                prompt = torch.cat(
                    [
                        prefix_i,  # (1, 1, dim)
                        class_i,   # (1, name_len, dim)
                        ctx_i,     # (1, n_ctx, dim)
                        suffix_i,  # (1, *, dim)
                    ],
                    dim=1,
                )
                prompts.append(prompt)
            prompts = torch.cat(prompts, dim=0)

        else:
            raise ValueError

        return prompts

class CustomCLIP(nn.Module):
    def __init__(self, args, classnames, clip_model):
        super().__init__()
        self.prompt_learner = PromptLearner(args, classnames, clip_model)  # TODO: 未使用??
        self.tokenized_prompts = self.prompt_learner.tokenized_prompts
        self.image_encoder = clip_model.visual
        self.text_encoder = TextEncoder(clip_model)                        # TODO: 未使用??
        self.logit_scale = clip_model.logit_scale
        self.dtype = clip_model.dtype

    def forward(self, image):
        image_features = self.image_encoder(image.type(self.dtype))

        prompts = self.prompt_learner()
        tokenized_prompts = self.tokenized_prompts
        text_features = self.text_encoder(prompts, tokenized_prompts)

        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

        logit_scale = self.logit_scale.exp()
        logits = logit_scale * image_features @ text_features.t()
        raise ValueError
        return logits


class UPT(nn.Module):
    """
    Unary-pairwise transformer

    Parameters:
    -----------
    detector: nn.Module
        Object detector (DETR)
    postprocessor: nn.Module 
        Postprocessor for the object detector
    interaction_head: nn.Module
        Interaction head of the network
    human_idx: int
        Index of the human class
    num_classes: int
        Number of action/interaction classes
    alpha: float
        Hyper-parameter in the focal loss
    gamma: float
        Hyper-parameter in the focal loss
    box_score_thresh: float
        Threshold used to eliminate low-confidence objects
    fg_iou_thresh: float
        Threshold used to associate detections with ground truth
    min_instances: float
        Minimum number of instances (human or object) to sample
    max_instances: float
        Maximum number of instances (human or object) to sample
    """
    def __init__(self,
        args,
        detector: nn.Module,
        postprocessor: nn.Module,
        model: nn.Module,
        origin_text_embeddings: torch.tensor,
        object_embedding: torch.tensor,
        human_idx: int, num_classes: int,
        alpha: float = 0.5, gamma: float = 2.0,
        box_score_thresh: float = 0.2, fg_iou_thresh: float = 0.5,
        min_instances: int = 3, max_instances: int = 15,
        object_class_to_target_class: List[list] = None,
        object_n_verb_to_interaction: List[list] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self.detector = detector
        self.postprocessor = postprocessor
        self.clip_head = model  # 具有 adapter 的 CLIP 模型
        self.origin_text_embeddings = origin_text_embeddings  # verb 句子嵌入（经过 CLIP 文本编码器编码）
        self.object_embedding = object_embedding.cuda()       # Fix: 迁移到GPU上. object 句子嵌入（经过 CLIP 文本编码器编码）
        self.visual_output_dim = model.image_encoder.output_dim  # CLIP 图像编码器输出的维度. 512
        self.object_n_verb_to_interaction = np.asarray(
                                object_n_verb_to_interaction, dtype=float
                            )

        self.human_idx = human_idx      # 0
        self.num_classes = num_classes  # 117

        self.alpha = alpha  # 0.5
        self.gamma = gamma  # 0.2

        self.box_score_thresh = box_score_thresh  # 0.2
        self.fg_iou_thresh = fg_iou_thresh        # 0.5

        self.min_instances = min_instances  # 3
        self.max_instances = max_instances  # 15
        self.object_class_to_target_class = object_class_to_target_class  # 每个 object 可能对应的 verb 类别列表
        self.num_anno = kwargs["num_anno"]  # 每个 verb 类别对应的实例个数

        self.use_distill = args.use_distill          # False
        self.use_consistloss = args.use_consistloss  # False

        self.num_classes = num_classes               # 117
        self.use_multi_hot = args.use_multi_hot      # True
        self.obj_affordance = args.obj_affordance    # False

        self.feature = []
        if 'HO' in args.logits_type:
            self.feature.append('hum_obj')
        if 'U' in args.logits_type or 'T' in args.logits_type:
            self.feature.append('uni')
        self.feature = '_'.join(self.feature)  # hum_obj_uni
        self.logits_type = args.logits_type    # HO+U+T

        num_shot = args.num_shot  # 2
        file1 = args.file1        # 'hicodet_pkl_files/union_embeddings_cachemodel_crop_padding_zeros_vitb16.p'
        # self.annotation_clip = pickle.load(open(file1,'rb'))

        if args.zs:  # default False
            self.zs_type = args.zs_type
            self.filtered_hoi_idx = hico_unseen_index[self.zs_type]
        else:
            self.filtered_hoi_idx = []
            self.zs_type = None

        self.unseen_verb_idxs = []
        self.label_choice = args.label_choice
        if 'HO' in self.logits_type:  # default True
            self.cache_model_HO, self.one_hots_HO, self.sample_lens_HO = self.load_cache_model(file1=file1, feature='hum_obj',num_classes=self.num_classes, num_shot=num_shot, filtered_hoi_idx = self.filtered_hoi_idx, use_multi_hot=self.use_multi_hot, label_choice=self.label_choice, num_anno=self.num_anno)
            self.cache_model_HO, self.one_hots_HO, self.sample_lens_HO  = self.cache_model_HO.cuda().float(), self.one_hots_HO.cuda().float(), self.sample_lens_HO.cuda().float()
        if 'U' in self.logits_type:   # default True
            self.cache_model_U, self.one_hots_U, self.sample_lens_U = self.load_cache_model(file1=file1, feature='uni',num_classes=self.num_classes, num_shot=num_shot, filtered_hoi_idx = self.filtered_hoi_idx, use_multi_hot=self.use_multi_hot, label_choice=self.label_choice, num_anno=self.num_anno)
            self.cache_model_U, self.one_hots_U, self.sample_lens_U = self.cache_model_U.cuda().float(), self.one_hots_U.cuda().float(), self.sample_lens_U.cuda().float()

        if self.num_classes == 117:
            self.seen_verb_idxs = [i for i in range(self.num_classes) if i not in self.unseen_verb_idxs]
        elif self.num_classes == 600:
            self.seen_hoi_idxs = [i for i in range(self.num_classes) if i not in self.filtered_hoi_idx]

        self.individual_norm = True
        self.logits_type = args.logits_type  # HO+U+T
        self.consist = True
        self.evaluate_type = 'detr' # gt, detr
        
        self.use_type = 'crop'
        self.beta_cache = torch.tensor(10)
        self.alpha_cache = torch.tensor(1.0)

        self.prior_type = args.prior_type  # default cbe
        self.finetune_adapter = True
        # c 表示边界框置信度分数(维度: 1); b 表示边界框(维度: 4); e 表示物体类别对应的句子的嵌入向量(维度: 512)
        if self.prior_type == 'cbe':  # default True
            self.priors_initial_dim = self.visual_output_dim+5  # 512 + 1 + 4
        elif self.prior_type == 'cb':
            self.priors_initial_dim = 5
        elif self.prior_type == 'ce':
            self.priors_initial_dim = self.visual_output_dim+1
        elif self.prior_type == 'be':
            self.priors_initial_dim = self.visual_output_dim+4
        elif self.prior_type == 'c':
            self.priors_initial_dim = 1
        elif self.prior_type == 'b':
            self.priors_initial_dim = 4
        elif self.prior_type == 'e':
            self.priors_initial_dim = self.visual_output_dim
        else:
            raise NotImplementedError

        self.use_weight_pred = args.use_weight_pred  # default False
        if self.finetune_adapter:  # default True
            if 'HO' in self.logits_type:  # default True
                # self.adapter_HO = nn.Linear(self.visual_output_dim * 2, self.cache_models.shape[0], bias=True)
                self.adapter_HO_weight = nn.Parameter(self.cache_model_HO.clone().detach())
                self.adapter_HO_bias = nn.Parameter(-torch.ones(self.cache_model_HO.shape[0]))
                self.label_HO = nn.Parameter(self.one_hots_HO, requires_grad=False)  # label 是不可学习的
                if not self.use_weight_pred:  # default True
                    self.logit_scale_HO = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))  # 可学习的缩放因子

            if 'U' in self.logits_type:  # default True
                # self.adapter_U = nn.Linear(self.visual_output_dim, self.cache_models.shape[0], bias=True)
                self.adapter_U_weight = nn.Parameter(self.cache_model_U.clone().detach())
                self.adapter_U_bias = nn.Parameter(-torch.ones(self.cache_model_U.shape[0]))
                self.label_U = nn.Parameter(self.one_hots_U, requires_grad=False)  # label 是不可学习的
                if not self.use_weight_pred:  # default True
                    self.logit_scale_U = nn.Parameter(torch.ones([]) * np.log(1 / 0.07)) 

            if 'T' in self.logits_type:  # default True, T 表示 verb 文本嵌入, 即论文中的 Domain-Agnostic Knowledge
                # self.adapter_union = nn.Linear(self.visual_output_dim, self.num_classes, bias=(args.zs == False))
                self.adapter_union_weight = nn.Parameter(self.origin_text_embeddings.clone().detach())  # verb 句子嵌入（经过 CLIP 文本编码器编码）
                if not self.use_weight_pred:  # default True
                    self.logit_scale_text = nn.Parameter(torch.ones([]) * np.log(1 / 0.07)) 
        
        if args.use_insadapter:  # default True
            if args.prior_method == 0:  # default True
                self.priors_downproj = MLP(self.priors_initial_dim, 128, 64, 3) # 512+4+1 --> 64
            elif args.prior_method == 1:
                self.priors_downproj = MLP(self.priors_initial_dim * 2, 128, 64, 3) # old 512+5   
            elif args.prior_method == 2:
                self.learnable_prior = nn.Parameter(torch.empty(args.vis_prompt_num, 64))
                nn.init.xavier_normal_(self.learnable_prior)

        # 600 个 HOI 类别中，属于 no_interaction 类别的编号
        self.no_interaction_indexes = [9, 23, 30, 45, 53, 64, 75, 85, 91, 95, 106, 110, 128, 145, 159, 169, 173, 185, 193, 197, 207, 213, 223, 231, 234, 238, 242, 246, 251, 256, 263, 272, 282, 289, 294, 304, 312, 324, 329, 335, 341, 347, 351, 355, 362, 367, 375, 382, 388, 392, 396, 406, 413, 417, 428, 433, 437, 444, 448, 452, 462, 473, 482, 487, 501, 505, 515, 527, 532, 537, 545, 549, 557, 561, 566, 575, 583, 587, 594, 599]
        # HOI_IDX_TO_OBJ_IDX[i] 表示编号为 i 的 HOI 类别对应的 object 类别编号
        self.HOI_IDX_TO_OBJ_IDX = [
                4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 14,
                14, 14, 14, 14, 14, 14, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 39,
                39, 39, 39, 39, 39, 39, 39, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 2, 2, 2, 2, 2,
                2, 2, 2, 2, 2, 2, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 56, 56, 56, 56,
                56, 56, 57, 57, 57, 57, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 19, 60, 60,
                60, 60, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16,
                16, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 3,
                3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 58,
                58, 58, 58, 18, 18, 18, 18, 18, 18, 18, 18, 18, 18, 18, 18, 6, 6, 6, 6, 6,
                6, 6, 6, 62, 62, 62, 62, 47, 47, 47, 47, 47, 47, 47, 47, 47, 47, 24, 24,
                24, 24, 24, 24, 46, 46, 46, 46, 46, 46, 46, 46, 46, 46, 34, 34, 34, 34, 34,
                34, 34, 34, 35, 35, 35, 21, 21, 21, 21, 59, 59, 59, 59, 13, 13, 13, 13, 73,
                73, 73, 73, 73, 45, 45, 45, 45, 45, 50, 50, 50, 50, 50, 50, 50, 55, 55, 55,
                55, 55, 55, 55, 55, 55, 51, 51, 51, 51, 51, 51, 51, 51, 51, 51, 67, 67, 67,
                67, 67, 67, 67, 74, 74, 74, 74, 74, 41, 41, 41, 41, 41, 41, 41, 41, 41, 41,
                54, 54, 54, 54, 54, 54, 54, 54, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20,
                20, 10, 10, 10, 10, 10, 42, 42, 42, 42, 42, 42, 29, 29, 29, 29, 29, 29, 23,
                23, 23, 23, 23, 23, 78, 78, 78, 78, 26, 26, 26, 26, 52, 52, 52, 52, 52, 52,
                52, 66, 66, 66, 66, 66, 33, 33, 33, 33, 33, 33, 33, 33, 43, 43, 43, 43, 43,
                43, 43, 63, 63, 63, 63, 63, 63, 68, 68, 68, 68, 64, 64, 64, 64, 49, 49, 49,
                49, 49, 49, 49, 49, 49, 49, 69, 69, 69, 69, 69, 69, 69, 12, 12, 12, 12, 53,
                53, 53, 53, 53, 53, 53, 53, 53, 53, 53, 72, 72, 72, 72, 72, 65, 65, 65, 65,
                48, 48, 48, 48, 48, 48, 48, 76, 76, 76, 76, 71, 71, 71, 71, 36, 36, 36, 36,
                36, 36, 36, 36, 36, 36, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 31, 31,
                31, 31, 31, 31, 31, 31, 31, 44, 44, 44, 44, 44, 32, 32, 32, 32, 32, 32, 32,
                32, 32, 32, 32, 32, 32, 32, 11, 11, 11, 11, 28, 28, 28, 28, 28, 28, 28, 28,
                28, 28, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 37, 77, 77, 77, 77, 77,
                38, 38, 38, 38, 38, 27, 27, 27, 27, 27, 27, 27, 27, 70, 70, 70, 70, 61, 61,
                61, 61, 61, 61, 61, 61, 79, 79, 79, 79, 9, 9, 9, 9, 9, 7, 7, 7, 7, 7, 7, 7,
                7, 7, 25, 25, 25, 25, 25, 25, 25, 25, 75, 75, 75, 75, 40, 40, 40, 40, 40,
                40, 40, 22, 22, 22, 22, 22
            ]
        # obj_to_no_interaction[i] 表示编号为 i 的 object 对应的 no_interaction HOI 类别编号
        self.obj_to_no_interaction = torch.as_tensor([169, 23, 75, 159, 9, 64, 193, 575, 45, 566, 329, 505, 417, 246,
                                                        30,  85, 128, 145, 185, 106, 324, 238, 599, 347, 213, 583, 355, 545,
                                                        515, 341, 473, 482, 501, 375, 231, 234, 462, 527, 537,  53, 594, 304,
                                                        335, 382, 487, 256, 223, 207, 444, 406, 263, 282, 362, 428, 312, 272,
                                                        91,  95, 173, 242, 110, 557, 197, 388, 396, 437, 367, 289, 392, 413,
                                                        549, 452, 433, 251, 294, 587, 448, 532, 351, 561])

        self.epoch = 0
        # self.use_deformable_attn = args.use_deformable_attn
        self.COCO_CLASSES = ['N/A', 'person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck', 'boat', 'traffic light', \
                    'fire hydrant','N/A', 'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow', 'elephant',\
                    'bear', 'zebra', 'giraffe', 'N/A', 'backpack', 'umbrella', 'N/A', 'N/A', 'handbag', 'tie', 'suitcase', 'frisbee', 'skis', \
                    'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard', 'tennis racket', 'bottle', \
                    'N/A', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple', 'sandwich', 'orange', 'broccoli', 'carrot', \
                    'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch', 'potted plant', 'bed', 'N/A', 'dining table', 'N/A', 'N/A', 'toilet', \
                    'N/A', 'tv', 'laptop', 'mouse', 'remote', 'keyboard', 'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator', \
                    'N/A', 'book', 'clock', 'vase', 'scissors', 'teddy bear', 'hair drier', 'toothbrush']
        # reserve_indices 是 hicodet 中的 80 个 object 类别在 COCO 类别中的编号
        self.reserve_indices = [idx for (idx, name) in enumerate(self.COCO_CLASSES) if name != 'N/A']
        self.reserve_indices = self.reserve_indices + [91]  # 新增一个编号为 91 的类别(nothing)
        self.reserve_indices = torch.as_tensor(self.reserve_indices)
        self.dataset = args.dataset  # hicodet
        self.hyper_lambda = args.hyper_lambda  # 2.8
        self.pseudo_label = args.pseudo_label  # False
        self.tpt = args.tpt  # False
        self.featmap_dropout = nn.Dropout(0.2)
        self.feat_mask_type = args.feat_mask_type  # 0: dropout(random mask); 1: None; default 0
        self.language_aware = args.LA  # False
        self.use_insadapter = args.use_insadapter  # True
        self.prior_method = args.prior_method      # 0: instance-wise, 1: pair-wise, 2: learnable; default 0
        self.LA_weight = args.LA_weight  # 0.6
        self.box_proj = args.box_proj    # 0: None; 1: f_u = ROI-feat + MLP(uni-box); defafult 0
        if self.box_proj:  # default False
            self.box_proj_mlp = MLP(8, 128, self.visual_output_dim, num_layers=3)
        if self.use_weight_pred:  # default False
            num_branch = len(self.logits_type.split('+'))
            self.weight_pred = Weight_Pred(input_dim=self.visual_output_dim*3, output_dim=num_branch)
        if self.obj_affordance:  # default False
            self.obj_affordance_query = nn.Parameter(torch.empty(1, self.visual_output_dim, dtype=self.clip_head.dtype))  # to be optimized
            self.obj_affordance_learner = nn.MultiheadAttention(embed_dim=512*1, num_heads=1, dropout=0.3, batch_first=True)
        # if self.dataset == 'swig':
        #     self.verb2interaction = torch.as_tensor(kwargs["verb2interaction"])
        self.use_mlp_proj = kwargs["use_mlp_proj"]  # default False
        if self.use_mlp_proj:  # default False
            self.mlp_proj = MLP(512, 512, 512, 3)


    def get_attention_feature(self, query_feat, human_feat, object_feat, ftype='patch'):  ## xxx
        
        device = torch.device('cuda')
        
        human_feat = human_feat.flatten(2).to(device)
        object_feat = object_feat.flatten(2).to(device)
        key_feat = torch.cat([human_feat,object_feat],dim=-1)

        query_feat = query_feat.flatten(2).transpose(1,2).to(device)
        
        global_feat = query_feat.mean(1)
        # key_feat = key_feat/key_feat.norm(dim=1, keepdim=True)
        # query_feat = query_feat/query_feat.norm(dim=-1, keepdim=True)

        weight_matrix = torch.bmm(query_feat, key_feat)
        weight_matrix = weight_matrix.float().softmax(-1)
        weight_query_feat = torch.bmm(weight_matrix, key_feat.transpose(1, 2).float()).mean(1)
        query_feat = weight_query_feat.half()
        return query_feat.cpu()


    @torch.no_grad()
    def refresh_unseen_verb_cache_mem(self, ):
        if self.num_classes == 117:
            seen_idxs = self.seen_verb_idxs
            unseen_idxs = self.unseen_verb_idxs
        elif self.num_classes == 600:
            seen_idxs = self.seen_hoi_idxs
            unseen_idxs = self.filtered_hoi_idx
        
        text_embedding = self.origin_text_embeddings
        cumsum_sample_lens = torch.cumsum(self.sample_lens, dim=-1) 

        tmp_emb = self.cache_models[cumsum_sample_lens[torch.as_tensor(seen_idxs)] - 1] ## -1: for simplicity
        for i in unseen_idxs:
            cur_logits = (text_embedding[i] @ text_embedding[torch.as_tensor(seen_idxs)].T)
            cur_logits = F.softmax(cur_logits)
            cur_emb = cur_logits @ tmp_emb
            start_idx = cumsum_sample_lens[i-1] if i > 0 else 0
            end_idx = cumsum_sample_lens[i]
            self.cache_models[start_idx:end_idx, :] = cur_emb
        
        if 'HO' in self.logits_type:
            self.adapter_HO_weight = nn.Parameter(self.cache_models[:, :self.visual_output_dim * 2].clone().detach())
        if 'U' in self.logits_type:
            self.adapter_U_weight = nn.Parameter(self.cache_models[:, -self.visual_output_dim:].clone().detach())

    def load_cache_model(self,file1, feature='uni',num_classes=117, num_shot=10, filtered_hoi_idx=[], use_multi_hot=False, label_choice='random', num_anno=None):  ## √
        """
        TODO:
        注意: 该方法默认加载所有训练集中的 verb 对应的 union_embeddings, object_embeddings, human_embeddings, 
              并不会过滤掉 self.unseen_verb_idxs 中的动作类别
        """
        annotation = pickle.load(open(file1,'rb'))
        categories = num_classes
        union_embeddings = [[] for i in range(categories)]
        obj_embeddings = [[] for i in range(categories)]
        hum_embeddings = [[] for i in range(categories)]
        real_verbs = [[] for i in range(categories)]
        filenames = list(annotation.keys())
        verbs_iou = [[] for i in range(categories)] # contain 600hois or 117 verbs
        # hois_iou = [[] for i in range(len(hois))]
        each_filenames = [[] for i in range(categories)]

        # 获取训练集中每张图片中的所有人物对的 union_box、human_box、object_box 特征和其对应的 ground-truth verb 类别
        for file_n in filenames:
            anno = annotation[file_n]
            # dict_keys (['boxes_h', 'boxes_o', 'verbs', 'union_boxes', 'union_features', 'huamn_features''object_features''objects', 'global_feature'])
            if categories == 117 or categories == 24: verbs = anno['verbs']
            else: verbs = (self.object_n_verb_to_interaction[anno['objects'], anno['verbs']]).astype(int)

            # anno['real_verbs'][i] 是该图片中第 i 对人物对的动词类别的 one-hot 编码
            num_ho_pair = len(anno['boxes_h'])  # 该图片中的人物对数量
            anno['real_verbs'] = np.zeros(shape=(num_ho_pair, categories))
            for i in range(num_ho_pair):
                anno['real_verbs'][i][verbs[i]] = 1
            
            # TODO: multi_hot 合理吗?? 
            # 如果图片中有两个不同的人物对，他们之间的 IoU 大于 0.6，例如 person1 eat orange1 和 person2 hold  orange2,
            # person1 和 person2 的 IoU 大于 0.6, orange1 和 orange2 的 IoU 大于 0.6,
            # 那么经过下面这段代码处理后，就会得到 person1 eat and hold orange1, person2 eat and hold orange2 这两个具有多个 verb 类别的人物对
            # 这合理吗？？
            if use_multi_hot:  # default True
                boxes_h_iou = torchvision.ops.box_iou(torch.as_tensor(anno['boxes_h']), torch.as_tensor(anno['boxes_h']))
                boxes_o_iou = torchvision.ops.box_iou(torch.as_tensor(anno['boxes_o']), torch.as_tensor(anno['boxes_o']))
                for i in range(num_ho_pair):
                    idx_h = boxes_h_iou[i] > 0.6
                    idx_o = torch.logical_and(boxes_o_iou[i] > 0.6, torch.as_tensor(anno['objects']) == anno['objects'][i])
                    idx_ho = torch.logical_and(idx_h, idx_o)
                    anno['real_verbs'][i] = torch.sum(torch.as_tensor(anno['real_verbs'])[idx_ho], dim=0)
                
                anno['real_verbs'][anno['real_verbs']>1] = 1

            ious = torch.diag(box_iou(torch.as_tensor(anno['boxes_h']), torch.as_tensor(anno['boxes_o'])))
            if len(verbs) == 0:
                print(file_n)

            for i, v in enumerate(verbs):
                if 'hico' in file1: ## TODO ??? why vcoco list idx out of range
                    if num_classes == 117:
                        if anno['verbs'][i] not in self.object_class_to_target_class[anno['objects'][i]]:
                            continue
                    elif num_classes == 600:
                        if v in filtered_hoi_idx:
                            continue
                union_embeddings[v].append(anno['union_features'][i] / np.linalg.norm(anno['union_features'][i]))
                obj_embeddings[v].append(anno['object_features'][i] / np.linalg.norm(anno['object_features'][i]))
                hum_embeddings[v].append(anno['huamn_features'][i] / np.linalg.norm(anno['huamn_features'][i]))
                each_filenames[v].append(file_n)
                real_verbs[v].append(anno['real_verbs'][i])
                # add iou
                verbs_iou[v].append(ious[i])

        if num_classes == 117:  # default True
            # 检查是否所有 117 个 verb 类别都有对应的 union_embedding,
            # 如果有些 verb 没有对应的 union_embedding，则将该 verb 添加到 unseen verb 列表中
            for i in range(categories):
                if len(union_embeddings[i]) == 0:
                    self.unseen_verb_idxs.append(i)
            print('[INFO]: missing idxs of verbs:', self.unseen_verb_idxs)
            # 对于 unseen verb，则采用随机生成一个长度为 512 的 embedding 作为其 union_embedding、obj_embedding、hum_embedding
            for i in self.unseen_verb_idxs:
                for z in range(num_shot):  # 2
                    union_embeddings[i].append(np.random.randn(self.visual_output_dim))
                    obj_embeddings[i].append(np.random.randn(self.visual_output_dim))
                    hum_embeddings[i].append(np.random.randn(self.visual_output_dim))
        elif num_classes == 600:
            for i in filtered_hoi_idx:
                for z in range(num_shot):
                    union_embeddings[i].append(np.random.randn(self.visual_output_dim))
                    obj_embeddings[i].append(np.random.randn(self.visual_output_dim))
                    hum_embeddings[i].append(np.random.randn(self.visual_output_dim))
        ## re-implement cachemodel construction
        cache_models_lst, each_lens_lst = [], []
        real_verbs_lst = []
        if feature == 'hum_obj':
            indexes = np.arange(len(union_embeddings))
            for i, hum_emb, obj_emb, embeddings, real_v in tqdm(zip(indexes, hum_embeddings, obj_embeddings, union_embeddings, real_verbs)):
                hum_emb =  torch.as_tensor(np.array(hum_emb)).float()   
                obj_emb = torch.as_tensor(np.array(obj_emb)).float()
                real_v = torch.as_tensor(np.array(real_v))
                new_embeddings = torch.cat([hum_emb, obj_emb], dim=-1)
                new_embeddings = new_embeddings.cuda().float()

                num_to_select = min(hum_emb.shape[0], num_shot)
                
                if num_to_select < hum_emb.shape[0]:
                    if label_choice == 'random':  # default True
                        topk_idx = torch.randperm(new_embeddings.shape[0])[:num_to_select] 
                    elif label_choice == 'multi_first':
                        v_, topk_idx = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select)
                    elif label_choice == 'single_first':
                        v_, topk_idx = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select, largest=False)
                    elif label_choice == 'single+multi':
                        v_, topk_idx1 = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select//2, largest=True)
                        v_, topk_idx2 = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select//2, largest=False)
                        topk_idx = torch.cat((topk_idx1, topk_idx2))
                    elif label_choice == 'rare_first':
                        real_freq = real_v @ num_anno.to(torch.float64)
                        v_, topk_idx = torch.topk(real_freq, k=num_to_select, largest=False)
                    elif label_choice == 'non_rare_first':
                        real_freq = real_v @ num_anno.to(torch.float64)
                        v_, topk_idx = torch.topk(real_freq, k=num_to_select, largest=True)
                    elif label_choice == 'rare+non_rare':
                        real_freq = real_v @ num_anno.to(torch.float64)
                        v_, topk_idx1 = torch.topk(real_freq, k=num_to_select//2, largest=True)
                        v_, topk_idx2 = torch.topk(real_freq, k=num_to_select//2, largest=False)
                        topk_idx = torch.cat((topk_idx1, topk_idx2))
                    new_embeddings = new_embeddings[topk_idx]
                    real_v = real_v[topk_idx]
                
                cache_models_lst.append(new_embeddings)
                each_lens_lst.append(num_to_select)
                real_verbs_lst.append(real_v)
        elif feature == 'uni':
            indexes = np.arange(len(union_embeddings))
            for i, hum_emb, obj_emb, embeddings, real_v in tqdm(zip(indexes, hum_embeddings, obj_embeddings, union_embeddings, real_verbs)):
                uni_emb = torch.as_tensor(np.array(embeddings)).float()
                real_v = torch.as_tensor(np.array(real_v))
                new_embeddings = uni_emb
                new_embeddings = new_embeddings.cuda().float()

                num_to_select = min(uni_emb.shape[0], num_shot)

                if num_to_select < uni_emb.shape[0]:
                    if label_choice == 'random':  # default True
                        topk_idx = torch.randperm(new_embeddings.shape[0])[:num_to_select] 
                    elif label_choice == 'multi_first':
                        v_, topk_idx = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select)
                    elif label_choice == 'single_first':
                        v_, topk_idx = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select, largest=False)
                    elif label_choice == 'single+multi':
                        v_, topk_idx1 = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select//2, largest=True)
                        v_, topk_idx2 = torch.topk(torch.sum(real_v, dim=-1), k=num_to_select//2, largest=False)
                        topk_idx = torch.cat((topk_idx1, topk_idx2))
                    elif label_choice == 'rare_first':
                        real_freq = real_v @ num_anno.to(torch.float64)
                        v_, topk_idx = torch.topk(real_freq, k=num_to_select, largest=False)
                    elif label_choice == 'non_rare_first':
                        real_freq = real_v @ num_anno.to(torch.float64)
                        v_, topk_idx = torch.topk(real_freq, k=num_to_select, largest=True)
                    elif label_choice == 'rare+non_rare':
                        real_freq = real_v @ num_anno.to(torch.float64)
                        v_, topk_idx1 = torch.topk(real_freq, k=num_to_select//2, largest=True)
                        v_, topk_idx2 = torch.topk(real_freq, k=num_to_select//2, largest=False)
                        topk_idx = torch.cat((topk_idx1, topk_idx2))
                    new_embeddings = new_embeddings[topk_idx]
                    real_v = real_v[topk_idx]
                cache_models_lst.append(new_embeddings)
                each_lens_lst.append(num_to_select)
                real_verbs_lst.append(real_v)
        else:
            raise NotImplementedError
        
        cache_models = torch.cat(cache_models_lst, dim=0)  # 对于 hum_obj, 形状为 [n, 1024], 对于 uni, 形状应为 [n, 512]
        labels = torch.cat(real_verbs_lst, dim=0)          # 形状为 [n, 117], multi-hot 编码
        # torch.sum(labels, dim=0) 是每个verb类别对应的cache数量
        # 对于 mutil-hot 编码，一个cache可对应多个verb类别
        return cache_models, labels, torch.sum(labels, dim=0)

    def get_clip_feature(self,image):  ## xxx
        x = self.clip_model.visual.conv1(image.type(self.clip_model.dtype))
        
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
        x = torch.cat([self.clip_model.visual.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        x = x + self.clip_model.visual.positional_embedding.to(x.dtype)
        x = self.clip_model.visual.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND
        
        local_feat = self.clip_model.visual.transformer.resblocks[:11]((x,None))[0]
        # x = self.clip_model.visual.transformer(x)
        # x = x.permute(1, 0, 2)  # LND -> NLD
        return local_feat
    
    def _reset_parameters(self):  ## xxx
        for p in self.context_aware.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        for p in self.layer_norm.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def compute_prior_scores(self,
        x: Tensor, y: Tensor, scores: Tensor, object_class: Tensor
    ) -> Tensor:  ### √
        
        prior_h = torch.zeros(len(x), self.num_classes, device=scores.device)  # [ho_pairs_cnt, 117]
        prior_o = torch.zeros_like(prior_h)
        
        # Raise the power of object detection scores during inference
        p = 1.0 if self.training else self.hyper_lambda  # p 就是论文公式(1)中的lambda
        s_h = scores[x].pow(p)
        s_o = scores[y].pow(p)
        if self.dataset == 'swig':  # default False
            prior_h = s_h.unsqueeze(-1).repeat(1, self.num_classes)
            prior_o = s_o.unsqueeze(-1).repeat(1, self.num_classes)
            return torch.stack([prior_h, prior_o])
        # Map object class index to target class index
        # Object class index to target class index is a one-to-many mapping 
        target_cls_idx = [self.object_class_to_target_class[obj.item()]
            for obj in object_class[y]]
        # Duplicate box pair indices for each target class
        pair_idx = [i for i, tar in enumerate(target_cls_idx) for _ in tar]
        # Flatten mapped target indices
        flat_target_idx = [t for tar in target_cls_idx for t in tar]

        prior_h[pair_idx, flat_target_idx] = s_h[pair_idx]  # 公式(1) 中的 {s_h}^{\lambda}
        prior_o[pair_idx, flat_target_idx] = s_o[pair_idx]  # 公式(1) 中的 {s_o}^{\lambda}
        # prior_h[i] 中的所有非零值都是相等的，并且等于该边界框的置信度分数(DETR输出的结果)，prior_o 也同理
        return torch.stack([prior_h, prior_o])  # [2, ho_pairs_cnt, 117]
    
    def conditional_mask(self, mask_shape: tuple, uni_mask_coor, instance_mask_coor,):
        '''
        :params
            mask_shape: e.g., (7,7)
            instance_mask_coor: [x1, y1, x2, y2]
        '''
        num = len(uni_mask_coor)
        tmp_mask1, tmp_mask2 = torch.zeros((num, *mask_shape)), torch.zeros((num, *mask_shape))
        instance_mask_coor[:, 0] = (instance_mask_coor[:, 0] - uni_mask_coor[:, 0]) / (uni_mask_coor[:, 2] - uni_mask_coor[:, 0]) * mask_shape[0]
        instance_mask_coor[:, 2] = (instance_mask_coor[:, 2] - uni_mask_coor[:, 0]) / (uni_mask_coor[:, 2] - uni_mask_coor[:, 0]) * mask_shape[0]
        instance_mask_coor[:, 1] = (instance_mask_coor[:, 1] - uni_mask_coor[:, 1]) / (uni_mask_coor[:, 3] - uni_mask_coor[:, 1]) * mask_shape[1]
        instance_mask_coor[:, 3] = (instance_mask_coor[:, 3] - uni_mask_coor[:, 1]) / (uni_mask_coor[:, 3] - uni_mask_coor[:, 1]) * mask_shape[1]
        instance_mask_coor = instance_mask_coor.int()
        for i in range(num):
            tmp_mask1[i, instance_mask_coor[i, 0] : instance_mask_coor[i, 2], :] = 1
            tmp_mask2[i, :, instance_mask_coor[i, 1]: instance_mask_coor[i, 3]] = 1
        intersection = tmp_mask1.logical_and(tmp_mask2)
        return intersection

    def compute_roi_embeddings(self, features: OrderedDict, image_size: Tensor, region_props: List[dict]):
        device = features.device
        boxes_h_collated = []; boxes_o_collated = []
        prior_collated = []; object_class_collated = []
        # pairwise_tokens_collated = []
        attn_maps_collated = []
        all_logits = []

        img_h, img_w = image_size.unbind(-1)
        scale_fct = torch.stack([img_w, img_h, img_w, img_h], dim=1)

        gt_feats_collated = []    # 用不到
        pair_feats_collated = []  # 用不到
        gt_all_logits = []
        pair_logits = []
        pair_prior = []
        gt_labels = []
        for b_idx, props in enumerate(region_props):
            local_features = features[b_idx]  # [512, 14, 14]
            boxes = props['boxes']            # [boxes_cnt, 4]
            scores = props['scores']          # [boxes_cnt]
            labels = props['labels']          # [boxes_cnt]

            is_human = labels == self.human_idx
            n_h = torch.sum(is_human); n = len(boxes)
            # Permute human instances to the top
            if not torch.all(labels[:n_h]==self.human_idx):
                h_idx = torch.nonzero(is_human).squeeze(1)
                o_idx = torch.nonzero(is_human == 0).squeeze(1)
                perm = torch.cat([h_idx, o_idx])
                boxes = boxes[perm]; scores = scores[perm]
                labels = labels[perm]
            # Skip image when there are no valid human-object pairs
            if n_h == 0 or n <= 1:
                boxes_h_collated.append(torch.zeros(0, device=device, dtype=torch.int64))
                boxes_o_collated.append(torch.zeros(0, device=device, dtype=torch.int64))
                object_class_collated.append(torch.zeros(0, device=device, dtype=torch.int64))
                prior_collated.append(torch.zeros(2, 0, self.num_classes, device=device))
                continue

            # 组合人框和物框, 形成若干人物对
            # Get the pairwise indices
            x, y = torch.meshgrid(
                torch.arange(n, device=device),
                torch.arange(n, device=device)
            )
            # Valid human-object pairs
            x_keep, y_keep = torch.nonzero(torch.logical_and(x != y, x < n_h)).unbind(1)
            if len(x_keep) == 0:
                # Should never happen, just to be safe
                raise ValueError("There are no valid human-object pairs")
            x = x.flatten(); y = y.flatten()

            # 人物对中的人框、物框、联合框
            # extract single roi features
            sub_boxes = boxes[x_keep]
            obj_boxes = boxes[y_keep]
            lt = torch.min(sub_boxes[..., :2], obj_boxes[..., :2]) # left point
            rb = torch.max(sub_boxes[..., 2:], obj_boxes[..., 2:]) # right point
            union_boxes = torch.cat([lt,rb],dim=-1)

            # 使用 RoI-Align 从具有 Adapter 的 CLIP visual encoder 输出的特征图中获取人框、物框、联合框的特征
            # torchvision.ops.roi_align 的 spatial_scale 参数是输入特征图的尺度与输入边界框尺度的比例,
            # 在这里，输入特征图的尺度是 14x14，输入边界框坐标的尺度是 224x224，因此 spatial_scale = 14 / 224
            # 1 / (224 / 14) = 14 / 224
            spatial_scale = 1 / (image_size[0,0]/local_features.shape[1])
            # union_features = torchvision.ops.roi_align(local_features.unsqueeze(0),[union_boxes],output_size=(1, 1),spatial_scale=spatial_scale,aligned=True).flatten(2).mean(-1)
            # single_features = torchvision.ops.roi_align(local_features.unsqueeze(0),[boxes],output_size=(1, 1),spatial_scale=spatial_scale,aligned=True).flatten(2).mean(-1)
            # 联合框的特征 [ho_pair_cnt, 512, 7, 7], ho_pair_cnt 是人物对数量
            union_features = torchvision.ops.roi_align(local_features.unsqueeze(0),[union_boxes],output_size=(7, 7),spatial_scale=spatial_scale,aligned=True)
            # 人框、物框的特征 [boxes_cnt, 512, 7, 7]
            single_features = torchvision.ops.roi_align(local_features.unsqueeze(0),[boxes],output_size=(7, 7),spatial_scale=spatial_scale,aligned=True)

            if self.feat_mask_type == 0:  # default True
                union_features = self.featmap_dropout(union_features).flatten(2).mean(-1)
                single_features = self.featmap_dropout(single_features).flatten(2).mean(-1)
            elif self.feat_mask_type == 1:
                union_features = union_features.flatten(2).mean(-1)
                single_features = single_features.flatten(2).mean(-1)
            # 
            # if self.box_proj == 1:
            #     box_feat = self.box_proj_mlp(torch.cat((sub_boxes, obj_boxes), dim=-1))
            
            human_features = single_features[x_keep]   # 人框特征: [ho_pair_cnt, 512, 7, 7]
            object_features = single_features[y_keep]  # 物框特征: [ho_pair_cnt, 512, 7, 7]
            # default True, 对人框、物框、联合框的特征执行归一化过程
            if self.individual_norm: ## todo should use norm during finetuning?? 
                concat_feat_original = torch.cat([human_features,object_features, union_features],dim=-1)
                human_features = human_features / human_features.norm(dim=-1, keepdim=True)
                object_features = object_features / object_features.norm(dim=-1, keepdim=True)
                union_features = union_features / union_features.norm(dim=-1, keepdim=True)

                # TODO: 后文并没有使用到 concat_feat
                if self.feature == 'hum_obj_uni':  # default True
                    concat_feat = torch.cat([human_features, object_features, union_features],dim=-1) 
                elif self.feature == 'hum_obj':
                    concat_feat = torch.cat([human_features, object_features], dim=-1)
                elif self.feature == 'uni':
                    concat_feat = union_features
            else:
                concat_feat = torch.cat([human_features,object_features, union_features],dim=-1) 
                concat_feat = concat_feat/concat_feat.norm(dim=-1,keepdim=True) 

            if self.logits_type == 'HO+U+T':  # default True
                # 这里执行论文中的公式(2)，即 Concept-guided Memeory 中的计算过程
                phi_union_HO = torch.cat([human_features, object_features], dim=-1) @ self.adapter_HO_weight.T + self.adapter_HO_bias  # [ho_pair_cnt, cache_cnt]
                phi_union_U = union_features @ self.adapter_U_weight.T + self.adapter_U_bias   # [ho_pair_cnt, cache_cnt]
                logits_cache_HO = ((phi_union_HO @ self.label_HO) / self.sample_lens_HO) / 2   # phi_union_HO 是人框、物框特征拼接后计算得到的结果，故这里除以2; [ho_pair_cnt, 117]
                logits_cache_U = (phi_union_U @ self.label_U) / self.sample_lens_U  # [ho_pair_cnt, 117]
                logits_text = union_features @ self.adapter_union_weight.T          # [ho_pair_cnt, 117]
                
                if self.use_weight_pred:  # default False
                    logits_weights = self.weight_pred(torch.cat([human_features,object_features, union_features], dim=-1))
                    logits = logits_cache_HO * logits_weights[:, 0:1] + logits_cache_U * logits_weights[:, 1:2] + logits_text * logits_weights[:, 2:3]
                else:  # default True
                    logits = logits_cache_HO * self.logit_scale_HO + logits_cache_U * self.logit_scale_U + logits_text * self.logit_scale_text  # [ho_pair_cnt, 117]

            elif self.logits_type == 'HO+T':
                phi_union_HO = torch.cat([human_features, object_features], dim=-1) @ self.adapter_HO_weight.T + self.adapter_HO_bias
                logits_cache_HO = ((phi_union_HO @ self.label_HO) / self.sample_lens_HO) / 2
                logits_text = union_features @ self.adapter_union_weight.T
                if self.use_weight_pred:
                    logits_weights = self.weight_pred(torch.cat([human_features,object_features, union_features], dim=-1))
                    logits = logits_cache_HO * logits_weights[:, 0:1] + logits_text * logits_weights[:, 1:2]
                else:
                    logits = logits_cache_HO * self.logit_scale_HO + logits_text * self.logit_scale_text

            elif self.logits_type == 'HO+U':
                phi_union_HO = torch.cat([human_features, object_features], dim=-1) @ self.adapter_HO_weight.T + self.adapter_HO_bias
                phi_union_U = union_features @ self.adapter_U_weight.T + self.adapter_U_bias
                logits_cache_HO = ((phi_union_HO @ self.label_HO) / self.sample_lens_HO) / 2
                logits_cache_U = (phi_union_U @ self.label_U) / self.sample_lens_U
                if self.use_weight_pred:
                    logits_weights = self.weight_pred(torch.cat([human_features,object_features, union_features], dim=-1))
                    logits = logits_cache_HO * logits_weights[:, 0:1] + logits_cache_U * logits_weights[:, 1:2]
                else:
                    logits = logits_cache_HO * self.logit_scale_HO + logits_cache_U * self.logit_scale_U
            
            elif self.logits_type == 'HO':
                phi_union_HO = torch.cat([human_features, object_features], dim=-1) @ self.adapter_HO_weight.T + self.adapter_HO_bias
                logits_cache_HO = ((phi_union_HO @ self.label_HO) / self.sample_lens_HO) / 2
                if self.use_weight_pred:
                    logits_weights = self.weight_pred(torch.cat([human_features,object_features, union_features], dim=-1))
                    logits = logits_cache_HO * logits_weights[:, 0:1]
                else:
                    logits = logits_cache_HO * self.logit_scale_HO
            
            elif self.logits_type == 'U':
                phi_union_U = union_features @ self.adapter_U_weight.T + self.adapter_U_bias
                logits_cache_U = (phi_union_U @ self.label_U) / self.sample_lens_U
                if self.use_weight_pred:
                    logits_weights = self.weight_pred(torch.cat([human_features,object_features, union_features], dim=-1))
                    logits = logits_cache_U * logits_weights[:, 0:1]
                else:
                    logits = logits_cache_U * self.logit_scale_U
                
            elif self.logits_type == 'T':
                logits_text = union_features @ self.adapter_union_weight.T
                if self.use_weight_pred:
                    logits_weights = self.weight_pred(torch.cat([human_features,object_features, union_features], dim=-1))
                    logits = logits_text * logits_weights[:, 0:1]
                else:
                    logits = logits_text * self.logit_scale_text
            
            boxes_h_collated.append(x_keep)  # boxes_h_collated[i] 表示当前批量中第i张图片中所有人物对的人框索引
            boxes_o_collated.append(y_keep)  # boxes_o_collated[i] 表示当前批量中第i张图片中所有人物对的物框索引
            object_class_collated.append(labels[y_keep])  # object_class_collated[i] 表示当前批量中第i张图片中所有人物对的物框类别编号
            prior_collated.append(self.compute_prior_scores(  # prior_collated[i] 表示当前批量中第i张图片中所有人物对的人、物边界框置信度分数，形状为 [ho_pairs_cnt, 117]
                x_keep, y_keep, scores, labels)
            )
            all_logits.append(logits)  # all_logits[i] 表示当前批量中第i张图片中所有人物对的动作置信度分数，即论文公式(1)中的 s_{h,o}^a，具体计算方式见公式(2); 形状为 [ho_pairs_cnt, 117]
            # all_interactiveness.append(interactiveness)
            # if self.training:
            # # match with gt pairs 
            #     gt_bx_h = self.recover_boxes(targets_region_props[b_idx]['boxes_h'], targets_region_props[b_idx]['size'])
            #     gt_bx_o = self.recover_boxes(targets_region_props[b_idx]['boxes_o'], targets_region_props[b_idx]['size'])
            #     # 哪些pred bbox和gt bbox匹配
            #     x_pair, y_pair = torch.nonzero(torch.min(
            #         box_iou(sub_boxes, gt_bx_h),
            #         box_iou(obj_boxes, gt_bx_o)
            #         ) >= (self.fg_iou_thresh)).unbind(1) ## todo :??fake match
                
            #     # print("x_lens_pair:",len(x_pair))
            #     gt_human_features = torch.as_tensor(self.annotation_clip[targets_region_props[b_idx]['filename']]['huamn_features']).to(device)[y_pair]
            #     gt_object_features = torch.as_tensor(self.annotation_clip[targets_region_props[b_idx]['filename']]['object_features']).to(device)[y_pair]
            #     gt_union_features = torch.as_tensor(self.annotation_clip[targets_region_props[b_idx]['filename']]['union_features']).to(device)[y_pair]
            #     if self.individual_norm:
            #         gt_concat_feat_original = torch.cat([gt_human_features,gt_object_features, gt_union_features],dim=-1)
            #         gt_human_features = gt_human_features/gt_human_features.norm(dim=-1, keepdim=True)
            #         gt_object_features = gt_object_features/gt_object_features.norm(dim=-1, keepdim=True)
            #         gt_union_features = gt_union_features/gt_union_features.norm(dim=-1, keepdim=True)
            #         gt_cancat_feat = torch.cat([gt_human_features,gt_object_features, gt_union_features],dim=-1)
            #     else:
            #         gt_cancat_feat = torch.cat([gt_human_features,gt_object_features, gt_union_features],dim=-1)
            #         gt_cancat_feat = gt_cancat_feat/gt_cancat_feat.norm(dim=-1, keepdim=True)
            #     gt_feats_collated.append(gt_cancat_feat)
            #     pair_feats_collated.append(concat_feat[x_pair])
            #     if self.use_consistloss:
            #         gt_phi_union = self.adapter(gt_cancat_feat) 
            #         gt_logits_cache = (gt_phi_union @ self.one_hots) / self.sample_lens 
            #         gt_logits_text = self.adapter_union(gt_union_features)
            #         gt_logits = gt_logits_cache  * self.logit_scale + gt_logits_text * self.logit_scale_text
            #         gt_all_logits.append(gt_logits)

            #         logits_pair = logits[x_pair]
            #         # x_, y_ = torch.nonzero(prior_collated[-1]).unbind(1)
            #         pair_logits.append(logits[x_pair])
            #         pair_prior.append(prior_collated[-1][x_pair])

            #         gt_label = torch.zeros(len(gt_logits), self.num_classes, device=gt_logits.device)
                    
            #         # gt_label[torch.arange(len(gt_logits)).to(device),torch.as_tensor(self.annotation_clip[targets_region_props[b_idx]['filename']]['hois']).to(device)[y_pair]] = 1
            #         # gt_labels.append(gt_label)
        
        if self.use_consistloss:  # default False
            pair_prior = torch.cat(pair_prior, dim=1).prod(0)
            x_, y_ = torch.nonzero(pair_prior).unbind(1)
            return all_logits, prior_collated, boxes_h_collated, boxes_o_collated, object_class_collated, gt_feats_collated, pair_feats_collated, gt_all_logits
        else:  # default True
            # gt_feats_collated, pair_feats_collated 实际上是空的，用不到
            return all_logits, prior_collated, boxes_h_collated, boxes_o_collated, object_class_collated, gt_feats_collated, pair_feats_collated
          
    def recover_boxes(self, boxes, size):  
        boxes = box_ops.box_cxcywh_to_xyxy(boxes)
        h, w = size
        scale_fct = torch.stack([w, h, w, h])
        boxes = boxes * scale_fct
        return boxes

    def associate_with_ground_truth(self, boxes_h, boxes_o, targets): ## for training
        n = boxes_h.shape[0]
        labels = torch.zeros(n, self.num_classes, device=boxes_h.device)  # [ho_pairs_cnt, 117]

        gt_bx_h = self.recover_boxes(targets['boxes_h'], targets['size'])
        gt_bx_o = self.recover_boxes(targets['boxes_o'], targets['size'])
        
        x, y = torch.nonzero(torch.min(  # 仅保留人框和物框均与ground-truth的IoU大于等于0.5的人物对
            box_iou(boxes_h, gt_bx_h),
            box_iou(boxes_o, gt_bx_o)
        ) >= self.fg_iou_thresh).unbind(1)
        # print("pair gt,",len(x),len(y))
        # IndexError: tensors used as indices must be long, byte or bool tensors
        if self.dataset == 'swig' and self.training:  # default False
            if len(y) > 0:
                tgthoi_y = torch.as_tensor([self.unique_hois[origin_hoi_idx.item()] for origin_hoi_idx in targets['hoi'][y]], device=boxes_h.device)
                labels[x, tgthoi_y] = 1
        elif self.num_classes == 117 or self.num_classes == 24 or self.num_classes == 407:
            labels[x, targets['labels'][y]] = 1  ## target['labels']: verb/action
        else:
            labels[x, targets['hoi'][y]] = 1
        # print("#(labels==1) = ", torch.sum(labels))
        return labels

    def compute_interaction_loss(self, boxes, bh, bo, logits, prior, targets, gt_feats, pair_feats,): ### loss
        """
        损失函数

        Args:
            - boxes[i] 表示当前批量中第i张图片中的所有边界框坐标，形状为 [boxes_cnt, 4]
            - bh[i] 表示当前批量中第i张图片中所有人物对的人框索引
            - bo[i] 表示当前批量中第i张图片中所有人物对的物框索引
            - logits[i] 表示当前批量中第i张图片中所有人物对的动作置信度分数，即论文公式(1)中的 s_{h,o}^a，具体计算方式见公式(2); 形状为 [ho_pairs_cnt, 117]
            - prior[i] 表示当前批量中第i张图片中所有人物对的人、物边界框置信度分数，形状为 [2, ho_pairs_cnt, 117]，即论文公式(1)中的 {s_h}^{\lambda} 和 {s_o}^{\lambda}
            - targets[i] 表示当前批量中第i张图片的 ground-truth
            - gt_feats 为空列表 []
            - pair_feats 为空列表 []
        """
        ## bx, bo: indices of boxes
        labels = torch.cat([
            self.associate_with_ground_truth(bx[h], bx[o], target)
            for bx, h, o, target in zip(boxes, bh, bo, targets)
        ])

        if self.pseudo_label and self.zs_type =='unseen_verb':  # default False
            W = (self.text_embedding[torch.as_tensor(self.seen_verb_idxs)] @ self.text_embedding[torch.as_tensor(self.unseen_verb_idxs)].T).to(labels.device)
            W = W.T
            W /= W.norm(dim=1, keepdim=True) ## 20 * 97
            labels[:, torch.as_tensor(self.unseen_verb_idxs).to(labels.device)] = labels[:, torch.as_tensor(self.seen_verb_idxs).to(labels.device)] @ W.T
        
        prior = torch.cat(prior, dim=1).prod(0)
        x, y = torch.nonzero(prior).unbind(1)
        num_one_label = torch.sum(labels)
        logits = torch.cat(logits) 
        logits = logits[x, y]; prior = prior[x, y]; labels = labels[x, y]
        
        n_p = len(torch.nonzero(labels))

        if dist.is_initialized():
            world_size = dist.get_world_size()
            n_p = torch.as_tensor([n_p], device='cuda')
            # n_p_distll = torch.as_tensor([n_p_distll], device='cuda')
            dist.barrier() 
            dist.all_reduce(n_p)
            n_p = (n_p / world_size).item()

            # dist.all_reduce(n_p_distll)
            # n_p_distll = (n_p_distll / world_size).item()
            # n_p = (n_p.true_divide(world_size)).item()
        
        loss = binary_focal_loss_with_logits(
            torch.log(
                prior / (1 + torch.exp(-logits) - prior) + 1e-8
            ), labels, reduction='sum',
            alpha=self.alpha, gamma=self.gamma
            )
        
        if self.use_distill:
            raise NotImplementedError
            # loss_feat = F.l1_loss(pair_feats, gt_feats,reduction='sum')/gt_feats.shape[1]
            loss_feat = torch.sum(3.0 - torch.diag(pair_feats @ gt_feats.t())) 
            return loss  / n_p + max((1-self.epoch * 0.05), 0) * loss_feat / n_p_distll
        else:
            return loss / n_p

    def prepare_region_proposals(self, results): ## √ detr extracts the human-object pairs
        """
        将目标检测器得到的box经过nms处理, 并按置信度进行过滤, 确保人框/物框的数量都在[min_instances, max_instances]范围内

        Args:
            results: list[dict], 是目标检测器输出结果，其列表长度等于批量大小，每一个字典元素含有三个键：
                    scores: 每个框的置信度分数
                    labels: 每个框的标签
                    boxes: 每个框的坐标
        Returns: list[dict]，其列表长度等于批量大小，每一个字典元素含有四个键：
                    boxes: 形状为(box_num, 4), 表示过滤后的所有边界框坐标
                    scores: 形状为(box_num,), 表示每个边界框的置信度分数
                    labels: 形状为(box_num,), 表示每个边界框的标签编号
        """
        region_props = []
        for res in results:
            sc, lb, bx = res.values()

            keep = batched_nms(bx, sc, lb, 0.5)
            sc = sc[keep].view(-1)
            lb = lb[keep].view(-1)
            bx = bx[keep].view(-1, 4)
            
            keep = torch.nonzero(sc >= self.box_score_thresh).squeeze(1)

            is_human = lb == self.human_idx
            hum = torch.nonzero(is_human).squeeze(1)
            obj = torch.nonzero(is_human == 0).squeeze(1)
            n_human = is_human[keep].sum(); n_object = len(keep) - n_human
            # Keep the number of human and object instances in a specified interval
            if n_human < self.min_instances:
                keep_h = sc[hum].argsort(descending=True)[:self.min_instances]
                keep_h = hum[keep_h]
            elif n_human > self.max_instances:
                keep_h = sc[hum].argsort(descending=True)[:self.max_instances]
                keep_h = hum[keep_h]
            else:
                keep_h = torch.nonzero(is_human[keep]).squeeze(1)
                keep_h = keep[keep_h]

            if n_object < self.min_instances:
                keep_o = sc[obj].argsort(descending=True)[:self.min_instances]
                keep_o = obj[keep_o]
            elif n_object > self.max_instances:
                keep_o = sc[obj].argsort(descending=True)[:self.max_instances]
                keep_o = obj[keep_o]
            else:
                keep_o = torch.nonzero(is_human[keep] == 0).squeeze(1)
                keep_o = keep[keep_o]

            keep = torch.cat([keep_h, keep_o])

            region_props.append(dict(
                boxes=bx[keep],
                scores=sc[keep],
                labels=lb[keep],
            ))

        return region_props

    def postprocessing(self, boxes, bh, bo, logits, prior, objects, image_sizes): ### √
        n = [len(b) for b in bh]
        logits = torch.cat(logits)
        logits = logits.split(n)

        detections = []
        for bx, h, o, lg, pr, obj, size,  in zip(
            boxes, bh, bo, logits, prior, objects, image_sizes,
        ):
            pr = pr.prod(0)
            x, y = torch.nonzero(pr).unbind(1)
            scores = torch.sigmoid(lg[x, y])
            
            detections.append(dict(
                boxes=bx, pairing=torch.stack([h[x], o[x]]),
                scores=scores * pr[x, y], labels=y,
                objects=obj[x], size=size
            ))

        return detections

    def get_obj_affordances(self, obj_labels, device):
        assert len(self.origin_text_embeddings) == 24 or len(self.origin_text_embeddings) == 117, "dim not match"
        bs = len(obj_labels); dim = self.origin_text_embeddings.shape[-1]
        verb_idxs = [self.object_class_to_target_class[obj] for obj in obj_labels]
        max_len = max([len(v) for v in verb_idxs])
        mask = torch.ones((bs, max_len),dtype=torch.bool,device=device)
        
        key = torch.zeros(bs, max_len, dim,device=device)
        for i in range(bs):
            cur_len = len(verb_idxs[i])
            key[i, :cur_len, :] = self.origin_text_embeddings[torch.as_tensor(verb_idxs[i])]
            mask[i, :cur_len] = False
        query = self.obj_affordance_query.unsqueeze(0).repeat(bs, 1, 1)
        obj_affordances, _ = self.obj_affordance_learner(query, key, key, key_padding_mask=mask)
        return obj_affordances

    def get_prior(self, region_props, image_size, prior_method): ##  for adapter module training
        """
        为图片生成先验知识，该方法对应到论文中的 Instance Knowledge.
        该方法为图片中的每一个box(由目标检测得到的box)生成一个先验向量, 先验向量维度为 517, 由三部分组成: 
            - 该边界框的置信度分数(c), 维度为 1;
            - 该边界框的坐标(b), 维度为 4;
            - 该边界框的类别文本嵌入(e), 维度为 512;
        先验向量经过 MLP 变换维度(517 --> 64), 从而得到维度为 64 的先验向量
            
        Args:
            region_props: 目标检测的结果
            image_size: 是 region_props 中边界框对应的图片大小
            prior_method: default = 0, 表示 Instance-aware Adapter, 即论文中描述的方法
        """
        max_feat = self.priors_initial_dim  # 512 + 4 + 1
        max_length = max(rep['boxes'].shape[0] for rep in region_props)  # 当前批量中，每个图片中最多出现多少个边界框, 这里记为 len
        mask = torch.ones((len(region_props),max_length),dtype=torch.bool,device=region_props[0]['boxes'].device)  # 形状为 [8, len]
        priors = torch.zeros((len(region_props),max_length, max_feat), dtype=torch.float32, device=region_props[0]['boxes'].device)  # 形状为 [8, len, 517]
        img_h, img_w = image_size.unbind(-1)
        scale_fct = torch.stack([img_w, img_h, img_w, img_h], dim=1)
        
        for b_idx, props in enumerate(region_props):
            boxes = props['boxes'] / scale_fct[b_idx][None,:]  # 将边界框坐标归一化
            scores = props['scores']
            labels = props['labels']
            is_human = labels == self.human_idx
            n_h = torch.sum(is_human); n = len(boxes)
            if n_h == 0 or n <= 1:
                print(f"Warning in get_prior: human boxes = {n_h}, object boxes = {n}")
                # sys.exit()
            
            object_embs = self.object_embedding[labels]  # 物体类别文本嵌入
            if self.obj_affordance:  # default False
                affordance_embs = self.get_obj_affordances(labels, region_props[0]['boxes'].device)
                object_embs = affordance_embs.squeeze(1)

            mask[b_idx,:n] = False
            
            # c 表示边界框置信度分数(维度: 1); b 表示边界框(维度: 4); e 表示物体类别对应的句子的嵌入向量(维度: 512)
            if self.prior_type == 'cbe':  # default True
                # 1 + 4 + 512
                priors[b_idx,:n,:5] = torch.cat((scores.unsqueeze(-1),boxes),dim=-1)
                priors[b_idx,:n,5:self.visual_output_dim+5] = object_embs
                # priors[b_idx,:n,512+5:] = unary_tokens
            elif self.prior_type == 'cb':
                priors[b_idx,:n,:5] = torch.cat((scores.unsqueeze(-1),boxes),dim=-1)
            elif self.prior_type == 'ce':
                priors[b_idx,:n,:1] = scores.unsqueeze(-1)
                priors[b_idx,:n,1:self.visual_output_dim+1] = object_embs
            elif self.prior_type == 'be':
                priors[b_idx,:n,:4] = boxes
                priors[b_idx,:n,4:self.visual_output_dim+4] = object_embs
            elif self.prior_type == 'c':
                priors[b_idx,:n,:1] = scores.unsqueeze(-1)
            elif self.prior_type == 'b':
                priors[b_idx,:n,:4] = boxes
            elif self.prior_type == 'e':
                priors[b_idx,:n,:self.visual_output_dim] = object_embs
            else:
                raise NotImplementedError

        if prior_method == 0:  # default True
            priors = self.priors_downproj(priors)
        elif prior_method == 1:
            pair_wise_priors = []
            for b_idx, props in enumerate(region_props):
                boxes = props['boxes'] / scale_fct[b_idx][None,:]
                scores = props['scores']
                labels = props['labels']
                is_human = labels == self.human_idx
                n_h = torch.sum(is_human); n = len(boxes)
                if n_h == 0 or n <= 1:
                    pair_wise_priors.append(torch.zeros(0, 0), )
                    print(n_h,n)
                    continue
                instance_wise_prior = priors[b_idx, :n]
                # Get the pairwise indices
                x, y = torch.meshgrid(
                    torch.arange(n, device=instance_wise_prior.device),
                    torch.arange(n, device=instance_wise_prior.device)
                )
                # Valid human-object pairs
                x_keep, y_keep = torch.nonzero(torch.logical_and(x != y, x < n_h)).unbind(1)
                if len(x_keep) == 0:
                    # Should never happen, just to be safe
                    raise ValueError("There are no valid human-object pairs")
                
                # extract single roi features
                sub_prior = instance_wise_prior[x_keep]
                obj_prior = instance_wise_prior[y_keep]
                
                pair_wise_priors.append(torch.cat((sub_prior, obj_prior), dim=-1))
            
            max_length = max(p.shape[0] for p in pair_wise_priors)
            mask = torch.ones((len(region_props),max_length),dtype=torch.bool,device=region_props[0]['boxes'].device)
            priors = torch.zeros((len(region_props),max_length, max_feat*2), dtype=torch.float32, device=region_props[0]['boxes'].device)
            for b_idx, props in enumerate(region_props):
                num_pair = pair_wise_priors[b_idx].shape[0]
                if num_pair > 0:
                    mask[b_idx, :num_pair] = False
                    priors[b_idx, :num_pair] = pair_wise_priors[b_idx]
            priors = self.priors_downproj(priors)   
        elif prior_method == 2:
            priors = self.learnable_prior.unsqueeze(0).repeat(len(region_props), 1, 1)
            mask = torch.zeros((priors.shape[0], priors.shape[1]), dtype=torch.bool,device=region_props[0]['boxes'].device)

        return (priors, mask)
    
    def prepare_target_hois(self, targets, device):
        unique_hois, cnt = {}, 0
        tgt_ids = []
        for t in targets:
            for hoi in t["hoi"]:
                hoi_id = hoi.item()
                if self.training:
                    # Only consider the texts within each mini-batch
                    if hoi_id not in unique_hois:
                        unique_hois[hoi_id] = cnt
                        cnt += 1
                    tgt_ids.append(unique_hois[hoi_id])
                else:
                    # Consider all hois in the dataset
                    tgt_ids.append(hoi_id)
        tgt_ids = torch.as_tensor(tgt_ids, dtype=torch.int64, device=device)
        return unique_hois
    
    def forward(self,
        images: List[Tensor],
        targets: Optional[List[dict]] = None
    ) -> List[dict]:
        """
        Parameters:
        -----------
        images: List[Tensor]
            Input images in format (C, H, W)
        targets: List[dict], optional
            Human-object interaction targets

        Returns:
        --------
        results: List[dict]
            Detected human-object interactions. Each dict has the following keys:
            `boxes`: torch.Tensor
                (N, 4) Bounding boxes for detected human and object instances
            `pairing`: torch.Tensor
                (2, M) Pairing indices, with human instance preceding the object instance
            `scores`: torch.Tensor
                (M,) Interaction score for each pair
            `labels`: torch.Tensor
                (M,) Predicted action class for each pair
            `objects`: torch.Tensor
                (M,) Predicted object class for each pair
            `attn_maps`: list
                Attention weights in the cooperative and competitive layers
            `size`: torch.Tensor
                (2,) Image height and width
        """
        if not self.finetune_adapter:  # default False
            raise NotImplementedError
            if self.training and targets is None:
                raise ValueError("In training mode, targets should be passed")
            image_sizes = torch.as_tensor([
                im.size()[-2:] for im in images
            ], device=images[0].device)
            region_props = self.get_region_proposals(targets)  # exhaustively generate the human-object pairs from the detr results
            feat_local_old = self.clip_model.encode_image(images[0])
            feat_local = feat_local_old[:,1:,:].transpose(1,2).view(feat_local_old.shape[0],-1, 7, 7).float()
            cls_feature = feat_local_old[:,0,:]
            # use the gt crop
            
            if self.evaluate_type == 'gt':
                if self.use_type == 'crop':
                    logits, prior, bh, bo, objects, boxes = self.compute_roi_embeddings_targets(cls_feature.unsqueeze(0), image_sizes, targets)
                else: #### ignore 
                    logits, prior, bh, bo, objects = self.compute_roi_embeddings(feat_local, image_sizes, region_props)
            elif self.evaluate_type == 'detr':      
                logits, prior, bh, bo, objects, = self.compute_crop_embeddings(cls_feature.unsqueeze(0), image_sizes, region_props, targets)
                boxes = [r['boxes'] for r in region_props]
            # boxes = [r['boxes'] for r in region_props]
            
            if self.training:
                interaction_loss = self.compute_interaction_loss(boxes, bh, bo, logits, prior, targets, interactiveness)
                loss_dict = dict(
                    interaction_loss=interaction_loss
                )
                return loss_dict

            detections = self.postprocessing(boxes, bh, bo, logits, prior, objects, image_sizes)
            return detections
        else:
            if self.training and targets is None:
                raise ValueError("In training mode, targets should be passed")
            batch_size = len(images)
            images_orig = [im[0].float() for im in images]
            images_clip = [im[1] for im in images]
            device = images_clip[0].device
            image_sizes = torch.as_tensor([
                im.size()[-2:] for im in images_clip
            ], device=device)
            image_sizes_orig = torch.as_tensor([
                im.size()[-2:] for im in images_orig
                ], device=device)
            
            if self.dataset == 'swig':  # default False
                ## boxes should be xyxy in the origin whwh coordinate space
                outputs = self.detector(images_orig)
                detections = self.postprocessor(
                    outputs, image_sizes
                )
                results = detections
                # results = [{'scores': tgt["pred_box_scores"].squeeze(), 'labels': tgt["pred_labels"], 'boxes': box_ops.box_cxcywh_to_xyxy(tgt["pred_boxes"]) * scale_fct[idx, None, :]} for idx, tgt in enumerate(targets)]
            else:  # default True
                # 使用预训练的 DETR 执行目标检测过程
                if isinstance(images_orig, (list, torch.Tensor)):  # default True, 将所有图片 tensor 通过补 0 对齐到当前批量中最大的那个图片
                    images_orig = nested_tensor_from_tensor_list(images_orig)
                features, pos = self.detector.backbone(images_orig)  # detr 的 ResNet Backbone
                src, mask = features[-1].decompose()
                # assert mask is not None2
                hs, detr_memory = self.detector.transformer(self.detector.input_proj(src), mask, self.detector.query_embed.weight, pos[-1])
                outputs_class = self.detector.class_embed(hs) # 6x8x100x81 or 6x8x100x92, 图片中每个边界框的类别，维度6是transformer中各层的输出，通常仅使用最后一层的输出
                outputs_coord = self.detector.bbox_embed(hs).sigmoid() # 6x8x100x4, 图片中的边界框(每张图片有100个边界框) 
                if self.dataset == 'vcoco' and outputs_class.shape[-1] == 92:  # default False
                    outputs_class = outputs_class[:, :, :, self.reserve_indices]
                    assert outputs_class.shape[-1] == 81, 'reserved shape NOT match 81'
                
                results = {'pred_logits': outputs_class[-1], 'pred_boxes': outputs_coord[-1]}
                results = self.postprocessor(results, image_sizes)

            # 将目标检测器得到的box经过nms处理, 并按置信度进行过滤, 确保人框/物框的数量都在[min_instances, max_instances]范围内
            region_props = self.prepare_region_proposals(results)
            if self.use_insadapter:  # default True
                # 获取当前图片的先验信息
                priors = self.get_prior(region_props,image_sizes, self.prior_method) ## priors: (prior_feat, mask): (batch_size*14*64, batch_size*14)
            else: 
                priors = None
            # with amp.autocast(enabled=True):
            images_clip = nested_tensor_from_tensor_list(images_clip)
            # 执行具有 Instance-aware Adapter 的 CLIP visual encoder.
            # 8x512, 8x512x14x14, 8 是 batch_size
            feat_global, feat_local = self.clip_head.image_encoder(images_clip.decompose()[0], priors)
            if self.use_mlp_proj:  # default False
                feat_local = feat_local.permute(0,2,3,1) # 8x14x14x512
                feat_local = self.mlp_proj(feat_local)
                feat_local = feat_local.permute(0,3,1,2)

            if self.tpt:  # default False
                interaction_loss = self.compute_loss_tpt(feat_local, image_sizes, region_props, targets)
                loss_dict = dict(
                    interaction_loss=interaction_loss
                )
                return loss_dict

            if self.dataset == 'swig' and self.training:  # default False
                self.unique_hois = self.prepare_target_hois(targets=targets, device=device)
                self.num_classes = len(self.unique_hois)
            
            if self.use_consistloss:  # default False
                logits, prior, bh, bo, objects, gt_feats, pair_feats, gt_all_logits = self.compute_roi_embeddings(feat_local, image_sizes, region_props)
            else:  # default True
                # logits[i] 表示当前批量中第i张图片中所有人物对的动作置信度分数，即论文公式(1)中的 s_{h,o}^a，具体计算方式见公式(2); 形状为 [ho_pairs_cnt, 117]
                # prior[i] 表示当前批量中第i张图片中所有人物对的人、物边界框置信度分数，形状为 [2, ho_pairs_cnt, 117]，即论文公式(1)中的 {s_h}^{\lambda} 和 {s_o}^{\lambda}
                # bh[i] 表示当前批量中第i张图片中所有人物对的人框索引
                # bo[i] 表示当前批量中第i张图片中所有人物对的物框索引
                # objects[i] 表示当前批量中第i张图片中所有人物对的物框类别编号
                # gt_feats 为空列表 []
                # pair_feats 为空列表 []
                logits, prior, bh, bo, objects, gt_feats, pair_feats = self.compute_roi_embeddings(feat_local, image_sizes, region_props)
                gt_all_logits = None
            boxes = [r['boxes'] for r in region_props] 
            
            if self.training:
                # 损失函数
                interaction_loss = self.compute_interaction_loss(boxes, bh, bo, logits, prior, targets, gt_feats, pair_feats)
                loss_dict = dict(
                    interaction_loss=interaction_loss
                )
                # if interaction_loss.isnan():
                if self.language_aware:  # default False
                    self.origin_text_embeddings = self.origin_text_embeddings.to(self.adapter_union_weight.device)
                    # language_aware_loss = (1 - torch.diagonal((self.adapter_union_weight / self.adapter_union_weight.norm(dim=-1, keepdim=True)) @ self.origin_text_embeddings.T)).mean()
                    sim_matrix = (self.adapter_union_weight / self.adapter_union_weight.norm(dim=-1, keepdim=True)) @ self.origin_text_embeddings.T
                    language_aware_loss = nn.CrossEntropyLoss()(sim_matrix, torch.arange(sim_matrix.shape[0]).to(self.adapter_union_weight.device))
                    loss_dict['language_aware_loss'] = self.LA_weight * language_aware_loss
                return loss_dict
            if len(logits) == 0:
                print(targets)
                return None
            detections = self.postprocessing(boxes, bh, bo, logits, prior, objects, image_sizes)
            return detections


def get_multi_prompts(classnames):   ## https://github.com/openai/CLIP/blob/main/data/prompts.md, 
    templates = ['a photo of a person {}.',
                'a video of a person {}.',
                'a example of a person {}.',
                'a demonstration of a person {}.',
                'a photo of the person {}.',
                'a video of the person {}.',
                'a example of the person {}.', 
                'a demonstration of the person {}.',
                
                # 'a photo of a person during {}.',
                # 'a video of a person during {}.',
                # 'a example of a person during {}.',
                # 'a demonstration of a person during {}.',
                # 'a photo of the person during {}.',
                # 'a video of the person during {}.',
                # 'a example of the person during {}.',
                # 'a demonstration of the person during {}.',

                # 'a photo of a person performing {}.',
                # 'a video of a person performing {}.',
                # 'a example of a person performing {}.',
                # 'a demonstration of a person performing {}.',
                # 'a photo of the person performing {}.',
                # 'a video of the person performing {}.',
                # 'a example of the person performing {}.',
                # 'a demonstration of the person performing {}.',
                
                # 'a photo of a person practicing {}.',
                # 'a video of a person practicing {}.',
                # 'a example of a person practicing {}.',
                # 'a demonstration of a person practicing {}.',
                # 'a photo of the person practicing {}.',
                # 'a video of the person practicing {}.',
                # 'a example of the person practicing {}.',
                # 'a demonstration of the person practicing {}.',
                ]
    hico_texts = [' '.join(name.split(' ')[5:]) for name in classnames]
    all_texts_input = []
    for temp in templates:
        texts_input = torch.cat([clip.tokenize(temp.format(text)) for text in hico_texts ])
        all_texts_input.append(texts_input)
    all_texts_input = torch.stack(all_texts_input,dim=0)
    return all_texts_input

@torch.no_grad()
def get_origin_text_emb(args, clip_model, tgt_class_names, obj_class_names):
    use_templates = args.use_templates  # default False
    if use_templates == False:  # default
        text_inputs = torch.cat([clip.tokenize(classname) for classname in tgt_class_names])
    elif use_templates:
        text_inputs = get_multi_prompts(tgt_class_names)
        bs_t, nums, c = text_inputs.shape
        text_inputs = text_inputs.view(-1, c)

    with torch.no_grad():
        origin_text_embedding = clip_model.encode_text(text_inputs)
    if use_templates:  # default False
        origin_text_embedding = origin_text_embedding.view(bs_t, nums, -1).mean(0)

    origin_text_embedding = origin_text_embedding / origin_text_embedding.norm(dim=-1, keepdim=True) # text embeddings of hoi 117*512 or 600*512

    obj_text_inputs = torch.cat([clip.tokenize(obj_text) for obj_text in obj_class_names])
    with torch.no_grad():
        obj_text_embedding = clip_model.encode_text(obj_text_inputs)
        object_embedding = obj_text_embedding
        # obj_text_embedding = obj_text_embedding[hoi_obj_list,:]
    return origin_text_embedding, object_embedding  # TODO: 这里为什么不对 object_embedding 进行 normalization


def build_detector(args, class_corr, object_n_verb_to_interaction, clip_model_path, num_anno, verb2interaction=None):
    if args.d_detr:
        detr, _, postprocessors = build_model_d_detr(args)
    else:
        detr, _, postprocessors = build_model(args)
    if os.path.exists(args.pretrained):
        if dist.get_rank() == 0:
            print(f"Load weights for the object detector from {args.pretrained}")
        if 'e632da11' in args.pretrained:
            detr.load_state_dict(torch.load(args.pretrained, map_location='cpu')['model']) 
        else:
            detr.load_state_dict(torch.load(args.pretrained, map_location='cpu')['model_state_dict'])
    
    clip_state_dict = torch.load(clip_model_path, map_location="cpu").state_dict()
    # clip_model 是对 clip 中每层 vis encoder 添加了 adaper 后的模型
    clip_model = CLIP_models_adapter_prior2.build_model(state_dict=clip_state_dict, use_adapter=args.use_insadapter, adapter_pos=args.adapter_pos, adapter_num_layers=args.adapter_num_layers)
    if args.num_classes == 117:
        classnames = hico_verbs_sentence
    elif args.num_classes == 24:
        classnames = vcoco_verbs_sentence
    elif args.num_classes == 600:
        classnames = list(hico_text_label.hico_text_label.values())
    else:
        raise NotImplementedError
    model = CustomCLIP(args, classnames=classnames, clip_model=clip_model)

    obj_class_names = [obj[1] for obj in hico_text_label.hico_obj_text_label]

    if args.dataset == 'swig' and not args.LA and 'e' not in args.prior_type:
        origin_text_embeddings = None
        object_embedding = torch.rand(1000, 1)
    else:
        # classnames 是动词句子列表：["a photo of a person is <verbing> the object", ...]
        # obj_class_names 是 object 句子列表: ["a photo of a/an <object>", ...]
        # 使用 CLIP 的文本编码器对其进行编码，得到对应的 embeddings. origin_text_embeddings 形状为 [117, 512]; object_embedding 形状为 [81, 512], 最后一个 object 类别为 nothing.
        origin_text_embeddings, object_embedding = get_origin_text_emb(args, clip_model=clip_model, tgt_class_names=classnames, obj_class_names=obj_class_names)
        origin_text_embeddings = origin_text_embeddings.clone().detach()
        object_embedding = object_embedding.clone().detach()
    
    detector = UPT(args,
        detr, postprocessors['bbox'], model, origin_text_embeddings, object_embedding,
        human_idx=args.human_idx, num_classes=args.num_classes,
        alpha=args.alpha, gamma=args.gamma,
        box_score_thresh=args.box_score_thresh,
        fg_iou_thresh=args.fg_iou_thresh,
        min_instances=args.min_instances,
        max_instances=args.max_instances,
        # class_corr[i] 表示第 i 个 object 类别可以与哪些 verb 类别进行组合
        object_class_to_target_class=class_corr,
        # 形状为[80, 117]的二维列表，object_n_verb_to_interaction[i][j] 表示第 i 个 object 与第 j 个 verb 构成的 HOI 类别编号，
        # 如果第 i 个 object 与第 j 个 verb 不构成 HOI 类别，则该元素为 None
        object_n_verb_to_interaction=object_n_verb_to_interaction,
        # num_anno[i] 表示第 i 个 verb 对应的实例个数
        num_anno = num_anno,
        # verb2interaction = verb2interaction,
        use_mlp_proj = args.use_mlp_proj,  # default False
    )
    return detector

