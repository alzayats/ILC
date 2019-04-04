# code by Yunbo Zhou

# global var define
import tensorflow as tf
import numpy as np
import tensorflow.python.ops.gen_nn_ops
# from tensorflow import keras
import keras
import keras.backend as K
import keras.layers as KL
import keras.engine as KE
import keras.models as KM

from tensorflow.contrib.distributions import percentile
from feature_extractor import resnet_graph,BatchNorm
import re
from dataset import data_generator
C = 20 # pascolvoc-20
P = int(1.5 * C)
batch_size = 32

def peak_filter(input):
    N, H, W, C = K.int_shape(input)
    threshold = percentile(input, q=50, axis=(1,2))
    threshold = K.reshape(threshold, [tf.shape(input)[0], 1, 1, C])
    return threshold

class local_maxima(KE.Layer):
    def __init__(self, win_size,**kwargs):
        assert win_size % 2 == 1 # win_size must be odd
        self.win_size = win_size
        self.offset = (win_size - 1) // 2
        super(local_maxima, self).__init__(**kwargs)

    def call(self, inputs):
        padded_maps = tf.pad(inputs,
                             [[0,0],[self.offset,self.offset],[self.offset,self.offset],[0,0]],
                             mode='CONSTANT',
                             constant_values=float('-inf'))
        _, h, w, _ = K.int_shape(padded_maps)
        ele_maps = tf.reshape(tf.range(0,h * w,dtype=tf.int32),[1,h,w,1])
        ele_maps = ele_maps[:,self.offset:-self.offset,self.offset:-self.offset,:]
        _, indx = tf.nn.max_pool_with_argmax(padded_maps,
                                             [1,self.win_size,self.win_size,1],
                                             [1,1,1,1],
                                             padding='VALID')
        mask_peak = K.greater_equal(inputs, peak_filter(inputs))


        peak_maps = tf.where(tf.logical_and(mask_peak, K.equal(tf.cast(indx, tf.int32),ele_maps)),
                             inputs,tf.zeros_like(inputs))


        return peak_maps # (N, H, W, C)




def mse_fn(tchat_idx, num_gt_idx):
    loss = tf.losses.mean_squared_error(num_gt_idx, tchat_idx)

    return loss

def MSE_loss(tchat_b, set_gt_b, num_gt_b):
    """
    :param tchat_b: (N,C)
    :param set_gt_b: (N,C) A:0 S:1 Shat:2  -1不加入训练
    :param num_gt_b: (N,C) number of category
    :return:
    """
    # use batch-slice to achieve
    output_loss = []

    for i in range(batch_size):
        tchat = tchat_b[i]   # (C)
        set_gt = set_gt_b[i] # (C)
        num_gt = num_gt_b[i] # (C)


        AS_idx = tf.logical_or(tf.equal(set_gt, 0), tf.equal(set_gt, 1))# 筛选出AS集合
        AS_idx = tf.where(AS_idx) # where返回localtion
        tchat_idx = tf.gather_nd(tchat, AS_idx) # (C~)
        num_gt_idx = tf.gather_nd(num_gt, AS_idx)

        output = tf.cond(K.equal(tf.shape(tchat_idx)[0], 0),
                         lambda : K.constant(0.0),
                         lambda : mse_fn(tchat_idx, num_gt_idx))
        output_loss.append(output)


    return K.switch(tf.size(output_loss) > 0, tf.divide(tf.add_n(output_loss), float(batch_size)), K.constant(0.0))


def rank_fn(tchat_idx, t_bound):
    """
    :param tchat_idx: (C~)
    :param t_bound: 5
    :param set_gt_idx: (C~) A:0 S:1 Shat:2
    :return:
    """
    loss = K.maximum(t_bound - tchat_idx, 0)
    loss = tf.cast(loss, tf.float32)
    loss = tf.divide(K.sum(loss),tf.cast(tf.shape(tchat_idx)[0],dtype=tf.float32))
    return loss


def Rank_loss(tchat_b, set_gt_b):
    """
    :param tchat_b: (N,C)
    :param t_bound: 5
    :param set_gt_b: (N,C) A:0 S:1 Shat:2
    :return:
    """
    t_bound = K.constant(5.0)
    output_loss = []
    for i in range(batch_size):
        tchat = tchat_b[i]
        set_gt = set_gt_b[i]

        Shat_idx = tf.equal(set_gt, 2)
        Shat_idx = tf.where(Shat_idx)
        tchat_idx = tf.gather_nd(tchat, Shat_idx)

        output = tf.cond(tf.equal(tf.shape(tchat_idx)[0], 0),
                         lambda : K.constant(0.0),
                         lambda : rank_fn(tchat_idx, t_bound))
        output_loss.append(output)

    return K.switch(tf.size(output_loss) > 0, tf.divide(tf.add_n(output_loss), float(batch_size)), K.constant(0.0))

def logsigmoid(x):
    x = K.sigmoid(x)
    x = K.log(x)
    return x

def Class_loss(class_confidence, set_gt):
    """
    :param class_confidence: (N,C) 每个类的得分score
    :param set_gt:(N,C)  图中出现过的类为1，否则为0
    :return:
    """
    target = tf.cast(tf.logical_or(tf.equal(set_gt, 1),tf.equal(set_gt, 2)), dtype=tf.float32)
    loss = -1.0 * (target * logsigmoid(class_confidence) + (1 - target) * logsigmoid(-logsigmoid(class_confidence)))
    loss = K.mean(loss)
    return loss

def get_classconfidence_graph(peak_maps):
    """
    :param peak_maps: (N,H,W,C)
    :return:(N,C)
    """
    class_confidence = tf.reduce_mean(peak_maps, axis=(1,2))

    return class_confidence # (N,C)



def sp_1_fn(peak_maps, num_gt):
    """
    :param peak_maps: (H,W,C~)
    :param num_gt: (C~)
    :return:
    """
    out = np.zeros_like(num_gt)
    peak_maps = np.transpose(np.reshape(peak_maps, (-1, peak_maps.shape[2]))) # (C~, H*W)

    for i in range(num_gt.shape[0]):
        num = num_gt[i]
        peak_map = peak_maps[i] # (H*W)
        out[i] = np.sort(peak_map, kind='heapsort')[-int(num)]
    return out



def spatial_loss_p_fn(peak_maps, density_map, num_gt, S_idx):
    peak_maps_S = tf.gather(peak_maps, S_idx, axis=-1) # H,W,C~
    num_gt_S = tf.gather(num_gt, S_idx) # C~
    density_map_S = tf.gather(density_map, S_idx, axis=-1) # H,W,C~

    # 找C~个feature_maps中第num最大的数
    tc_peak_value = tf.py_func(sp_1_fn, [peak_maps_S, num_gt_S], tf.float32)
    tc_peak_value = tf.reshape(tc_peak_value, [1,1,tf.shape(num_gt_S)[0]])
    pseudo_gt = tf.where(tf.greater_equal(peak_maps_S - tc_peak_value, 0),
                         tf.ones_like(peak_maps_S, dtype=tf.float32),
                         tf.zeros_like(peak_maps_S, dtype=tf.float32))
    pseudo_gt_stop = tf.stop_gradient(pseudo_gt) # (h,w,c~)
    Dhatc = pseudo_gt_stop * density_map_S

    loss_p = tf.divide(K.sum(tf.nn.sigmoid_cross_entropy_with_logits(labels=pseudo_gt_stop, logits=Dhatc), axis=(0,1)), K.sum(pseudo_gt_stop, axis=(0,1)))
    loss_p = K.sum(tf.divide(loss_p, tf.cast(tf.shape(S_idx)[0], dtype=tf.float32)))
    return loss_p


def spatial_loss_n_fn(density_map, A_idx):
    density_map_A = tf.gather(density_map, A_idx, axis=-1) # H,W,C~
    pseudo_gt_n = tf.zeros_like(density_map_A)
    loss_n = K.mean(tf.nn.sigmoid_cross_entropy_with_logits(labels=pseudo_gt_n, logits=density_map_A), axis=(0,1))
    loss_n = K.sum(tf.divide(loss_n, tf.cast(tf.shape(A_idx)[0], dtype=tf.float32)))
    return loss_n

# gather 索引->选择值 axis
def spatial_loss(peak_maps_b, density_map_b, num_gt_b, set_gt_b):
    """

    because that each example S set is different!Fuck~
    so we should operate each example not batch
    :param peak_maps:(N,H,W,C)
            density_maps(N,H,W,C)
            num_gt_b :(N,C)
            set_gt_b : (N,C)  S:1
    :return:
    """
    out_sp_p = []
    out_sp_n = []
    for i in range(batch_size):
        peak_maps = peak_maps_b[i] # 生成pseudo-GT H,W,C
        density_map = density_map_b[i] # H,W,C
        num_gt = num_gt_b[i]
        set_gt = set_gt_b[i]

        S_idx = tf.equal(set_gt, 1)
        S_idx = tf.where(S_idx)[:, 0] # 第一维的索引

        loss_p = tf.cond(tf.equal(tf.shape(S_idx)[0], 0),
                    lambda : K.constant(0.0),
                    lambda : spatial_loss_p_fn(peak_maps, density_map,num_gt, S_idx))
        out_sp_p.append(loss_p)
        A_idx = tf.equal(set_gt, 0)
        A_idx = tf.where(A_idx)[:, 0]
        loss_n = tf.cond(tf.equal(tf.shape(A_idx)[0], 0),
                         lambda : K.constant(0.0),
                         lambda : spatial_loss_n_fn(density_map, A_idx))
        out_sp_n.append(loss_n)


    lossp = K.switch(tf.size(out_sp_p) > 0, tf.divide(tf.add_n(out_sp_p), float(batch_size)), K.constant(0.0))
    lossn = K.switch(tf.size(out_sp_n) > 0, tf.divide(tf.add_n(out_sp_n), float(batch_size)), K.constant(0.0))
    return tf.add(lossp, lossn)






def get_model():
    input = KL.Input(shape=[512,512,3], name='input_image') # 图像

    set_gt = KL.Input(shape=[C], name='input_set_gt') # 每个类别的所属集合 A:0 S:1 Shat:2 不加入训练:-1
    num_gt = KL.Input(shape=[C], name='input_num_gt') # 每个类别的实际个数


    # fuck feature extractor
    x = resnet_graph(input, 'resnet50', train_bn=True)
    x = KL.Conv2D(2 * P, (1, 1), strides=(1,1), name='ILC_conv_p', use_bias=True)(x)
    density_x = KL.Lambda(lambda x:x[..., :P])(x)
    img_x = KL.Lambda(lambda x:x[..., P:])(x)



    # density_branch
    density_x = BatchNorm(name='ILC_density_bn')(density_x, training=True)
    density_x = KL.Activation('relu')(density_x)
    density_map = KL.Conv2D(C, (1,1), strides=(1,1), name='ILC_density_conv')(density_x) # (N,H,W,C)
    tchat = KL.Lambda(lambda x: K.sum(x,axis=(1,2)))(density_map) # (N,C)




    # image_branch
    img_x = BatchNorm(name='ILC_image_bn')(img_x, training=True)
    img_x = KL.Activation('relu')(img_x)
    object_map = KL.Conv2D(C, (1,1), strides=(1,1),name='ILC_image_conv')(img_x)
    peak_maps = local_maxima(win_size=3)(object_map)
    class_confidence = KL.Lambda(lambda x: get_classconfidence_graph(x))(peak_maps)



    # global loss
    # MSE
    mse_loss = KL.Lambda(lambda x: MSE_loss(*x),
                         name='ILC_mse_loss')([tchat, set_gt, num_gt])
    # Rank
    rank_loss = KL.Lambda(lambda x:Rank_loss(*x),
                          name='ILC_rank_loss')([tchat, set_gt])

    # spatial loss
    fuckoff_sp_loss = KL.Lambda(lambda x: spatial_loss(*x),
                                name='ILC_spatial_loss')([peak_maps, density_map, num_gt, set_gt])


    # class loss
    class_loss = KL.Lambda(lambda x:Class_loss(*x),
                           name='ILC_class_loss')([class_confidence,set_gt])

    inputs = [input, num_gt, set_gt]
    outputs = [mse_loss, rank_loss, fuckoff_sp_loss, class_loss]
    return KM.Model(inputs, outputs, name='ILC')


#####################################################################################
### 训练配置 ###
###############################################################################

def compile(model,loss_name, lr=0.0001, clipnorm=True):
    if clipnorm:
        optimizer = keras.optimizers.SGD(lr=lr, momentum=0.9,decay=1e-4,
                                        clipnorm=5.0)
    else:
        optimizer = keras.optimizers.SGD(lr=lr, momentum=0.9,decay=1e-4)
    model._losses = []
    model._per_input_losses = {}
    for name in loss_name:
        layer = model.get_layer(name)
        if layer.output in model.losses:
            continue
        weight = 1.0
        if name == "ILC_rank_loss":
            weight = 0.1
        loss = (layer.output * weight)
        model.add_loss(loss)

    reg_losses = [keras.regularizers.l2(1e-4)(w) / tf.cast(tf.size(w), tf.float32)
                  for w in model.trainable_weights
                  if 'gamma' not in w.name and 'beta' not in w.name]
    model.add_loss(tf.add_n(reg_losses))

    model.compile(optimizer=optimizer, loss=[None] * 4)

    # add metrics for loss
    for name in loss_name:
        if name in model.metrics_names:
            continue

        layer = model.get_layer(name)
        model.metrics_names.append(name)
        weight = 1.0
        if name == "rank_loss":
            weight = 0.1
        loss = (weight * layer.output)
        model.metrics_tensors.append(loss)

def set_trainable(model, layer_regex, verbose=0):
    layers = model.layers

    for layer in layers:
        if not layer.weights:
            continue
        trainable = bool(re.fullmatch(layer_regex, layer.name))

        layer.trainable = trainable
        if trainable and verbose > 0:
            print(layer.name, layer.__class__.__name__)

def load_weights(file_path ,model ,by_name=False, exclude=None):
    import h5py
    from keras.engine import saving

    if exclude:
        by_name = True

    if h5py is None:
        raise ImportError("requires h5py.")
    f = h5py.File(file_path, mode='r')
    if 'layer_names' not in f.attrs and 'model_weights' in f:
        f = f['model_weights']

    layers = model.layers
    if exclude:
        layers = filter(lambda l: l.name not in exclude, layers)

    if by_name:
        saving.load_weights_from_hdf5_group_by_name(f, layers)
    else:
        saving.load_weights_from_hdf5_group(f, layers)
    if hasattr(f, 'close'):
        f.close()

def train():
    model = get_model()

    train_datagen = data_generator(batch_size=batch_size)

    loss_names_stage1 = ["ILC_mse_loss", "ILC_rank_loss", "ILC_class_loss"]
    loss_names_stage2 = ["ILC_mse_loss", "ILC_rank_loss", "ILC_class_loss", "ILC_spatial_loss"]
    regex_stage1 = r"(ILC\_.*)"
    regex_stage2 = ".*"
    log_dir = "./log"
    checkpoint_dir = "./checkpoints/weights.{epoch:03d}.hdf5"
    pretrained_dir = "./pretrained/resnet50_weights_tf_dim_ordering_tf_kernels_notop.h5"

    # train stage 1
    load_weights(pretrained_dir, model, by_name=True)
    set_trainable(model, regex_stage1, 1)
    compile(model, loss_names_stage1, lr=0.01,clipnorm=True)

    callbacks = [keras.callbacks.TensorBoard(log_dir=log_dir, histogram_freq=0,
                                            write_graph=True, write_images=False),
                 keras.callbacks.ModelCheckpoint(checkpoint_dir, verbose=0, mode="min",
                                                 save_weights_only=True)]

    model.fit_generator(
        train_datagen,
        epochs=30,
        steps_per_epoch=35,
        callbacks=callbacks,
        max_queue_size=100,
        workers=0,
        use_multiprocessing=True
    )

    # model.save_weights("./checkpoints/weights_stage1.hdf5")




    # # train stage 2
    # set_trainable(model, regex_stage2, 1)
    # compile(model, loss_names_stage2)
    #
    #
    #
    #
    # model.summary()
    # print('fuck')
    #




if __name__ == '__main__':
    train()
