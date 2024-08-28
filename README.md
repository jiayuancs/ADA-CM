# ADA-CM

精简版本的 ADA-CM：

- 去除了原始 ADA-CM 中的无用代码
- 添加了部分注释说明
- 未改变模型结构

Cache 说明（具体见代码 `upt_tip_cache_model_free_finetune_distill3.py/UPT.load_cache_model`）：

- 针对每个 verb, 从训练集中随机选择 num_shot(default = 2) 个属于该 verb 类别的 HOI 实例
    - 如果该 verb 类别的 HOI 实例个数小于 num_shot 个且大于 0 个，则选择全部的 HOI 实例
    - 如果该 verb 类别的 HOI 实例为 0 个，则不选择 HOI 实例
    - **注意**：load_cache_model 方法在这一步骤中没有过滤掉 unseen_verb 类别。因此即使 verb 属于 unseen_verb 类别，只要训练集中其对应的 HOI 实例个数不为 0，load_cache_model 方法默认也会从中选择该类别的 HOI 实例。
- 将选择到的 HOI 实例对应的人框特征、物框特征、联合框特征、verb 标签类别（multi-hot 编码）添加到 cache 中
- 对于 HOI 实例为 0 个的 verb 类别和 unseen_verb 类别，随机生成 num_shot 个向量作为其对应特征添加到 cache 中


Prior Knowledge 说明（具体见代码 `upt_tip_cache_model_free_finetune_distill3.py/UPT.get_prior`）：

- 该方法为图片中的每一个box(由目标检测得到的box)生成一个先验向量, 先验向量维度为 517, 由三部分组成: 
    - 该边界框的置信度分数(c), 维度为 1;
    - 该边界框的坐标(b), 维度为 4;
    - 该边界框的类别文本嵌入(e), 维度为 512;
- 先验向量经过 MLP 变换维度(517 --> 64), 从而得到维度为 64 的先验向量
