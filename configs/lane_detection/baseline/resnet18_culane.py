# Data pipeline
from configs.lane_detection.common.datasets.culane_seg import dataset
from configs.lane_detection.common.datasets.seg_level0_288 import train_augmentation
from configs.lane_detection.common.datasets.seg_test_288 import test_augmentation

# Optimization pipeline
from configs.lane_detection.common.optims.segloss_5class import loss
from configs.lane_detection.common.optims.sgd02 import optimizer
from configs.lane_detection.common.optims.ep12_warmup200 import lr_scheduler

# Default args that can be overridden in commandline
train_args_default = dict(
    exp_name='resnet18_baseline_culane',
    workers=10,
    batch_size=20,
    continue_from=None,
    # Device args
    world_size=0,
    dist_url='env://',
    device='cuda'
)
test_args_default = dict(
    exp_name='resnet18_baseline_culane',
    workers=10,
    batch_size=80,
    continue_from='resnet18_baseline_culane.pt',
    # Device args
    device='cuda'
)

# Configs
train = dict(
    input_size=(288, 800),
    original_size=(590, 1640),
    num_classes=5,
    num_epochs=12,
    collate_fn=None,  # 'dict_collate_fn' for LSTR
    seg=True,  # Seg-based method or not
    validation=False,  # Seg IoU validation (mostly useless)
    val_num_steps=0,
)
train.update(train_args_default)

test = dict(
    seg=True,
    gap=20,
    ppl=18,
    thresh=0.3,
    collate_fn=None,  # 'dict_collate_fn' for LSTR
    input_size=(288, 800),
    original_size=(590, 1640),
    max_lane=4,
    dataset_name='culane'
)
test.update(test_args_default)

# Essentially DeepLabV1 without dilation like in SCNN paper
model = dict(
    name='standard_segmentation_model',
    backbone_cfg=dict(
        name='predefined_resnet_backbone',
        backbone_name='resnet18',
        return_layer='layer4',
        pretrained=True,
        replace_stride_with_dilation=[False, True, True]
    ),
    reducer_cfg=dict(
        name='RESAReducer',
        in_channels=512,
        reduce=128
    ),
    classifier_cfg=dict(
        name='DeepLabV1Head',
        in_channels=128,
        num_classes=5,
        dilation=1
    ),
    lane_classifier_cfg=dict(
        name='SimpleLaneExist',
        num_output=5 - 1,
        flattened_size=4500
    )
)
