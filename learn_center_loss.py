import numpy as np

import argparse
import pickle
import os
import shutil
from collections import OrderedDict

import keras
from keras import backend as K

import utils
from datasets import DATASETS, get_data_generator



def center_loss_model(base_model, centroids):
    
    num_classes = centroids.shape[0] if isinstance(centroids, np.ndarray) else centroids
    
    input_ = base_model.input
    embedding = base_model.output
    
    prob = keras.layers.Activation('relu')(embedding)
    prob = keras.layers.BatchNormalization(name='embedding_bn')(prob)
    prob = keras.layers.Dense(num_classes, activation = 'softmax', name = 'prob')(prob)
    
    cls_input_ = keras.layers.Input((1,), name = 'labels')
    cls_embedding_layer = keras.layers.Embedding(num_classes, int(embedding.shape[-1]), name = 'cls_centroids')
    cls_embedding = cls_embedding_layer(cls_input_)
    if isinstance(centroids, np.ndarray):
        cls_embedding_layer.set_weights([centroids])
        cls_embedding_layer.trainable = False
    
    center_loss = keras.layers.subtract([
            keras.layers.Lambda(lambda x: x[:,None,:])(embedding),
            cls_embedding
        ], name = 'center_dist')
    center_loss = keras.layers.Lambda(lambda x: K.sum(K.square(x), axis = -1) / 2, name = 'center_loss')(center_loss)
    
    return keras.models.Model([input_, cls_input_], [prob, center_loss])


def transform_inputs(X, y, num_classes):
    
    return [X, y], [keras.utils.to_categorical(y, num_classes), np.zeros(len(X))]



if __name__ == '__main__':

    # Parse arguments
    parser = argparse.ArgumentParser(description = 'Learns image embeddings using softmax + center loss (Wen et al.).', formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    arggroup = parser.add_argument_group('Data parameters')
    arggroup.add_argument('--dataset', type = str, required = True, choices = DATASETS, help = 'Training dataset.')
    arggroup.add_argument('--data_root', type = str, required = True, help = 'Root directory of the dataset.')
    arggroup.add_argument('--class_list', type = str, default = None, help = 'Path to a file containing the IDs of the subset of classes to be used (as first words per line).')
    arggroup = parser.add_argument_group('Center loss parameters')
    arggroup.add_argument('--embed_dim', type = int, default = 100, help = 'Dimensionality of learned image embeddings.')
    arggroup.add_argument('--centroids', type = str, default = None, help = 'Path to a pickle dump of embeddings generated by compute_class_embeddings.py. If given, this fixed set of class centroids will be used instead of learning centroids.')
    arggroup.add_argument('--center_loss_weight', type = float, default = 0.1, help = 'Weight of the center loss (softmax loss has fixed weight 1.0).')
    arggroup = parser.add_argument_group('Training parameters')
    arggroup.add_argument('--architecture', type = str, default = 'simple', choices = utils.ARCHITECTURES, help = 'Type of network architecture.')
    arggroup.add_argument('--lr_schedule', type = str, default = 'SGDR', choices = utils.LR_SCHEDULES, help = 'Type of learning rate schedule.')
    arggroup.add_argument('--clipgrad', type = float, default = 10.0, help = 'Gradient norm clipping.')
    arggroup.add_argument('--max_decay', type = float, default = 0.0, help = 'Learning Rate decay at the end of training.')
    arggroup.add_argument('--epochs', type = int, default = None, help = 'Number of training epochs.')
    arggroup.add_argument('--batch_size', type = int, default = 100, help = 'Batch size.')
    arggroup.add_argument('--val_batch_size', type = int, default = None, help = 'Validation batch size.')
    arggroup.add_argument('--finetune', type = str, default = None, help = 'Path to pre-trained weights to be fine-tuned (will be loaded by layer name).')
    arggroup.add_argument('--finetune_init', type = int, default = 3, help = 'Number of initial epochs for training just the new layers before fine-tuning.')
    arggroup.add_argument('--gpus', type = int, default = 1, help = 'Number of GPUs to be used.')
    arggroup.add_argument('--read_workers', type = int, default = 8, help = 'Number of parallel data pre-processing processes.')
    arggroup.add_argument('--queue_size', type = int, default = 100, help = 'Maximum size of data queue.')
    arggroup.add_argument('--gpu_merge', action = 'store_true', default = False, help = 'Merge weights on the GPU.')
    arggroup = parser.add_argument_group('Output parameters')
    arggroup.add_argument('--model_dump', type = str, default = None, help = 'Filename where the learned model definition and weights should be written to.')
    arggroup.add_argument('--weight_dump', type = str, default = None, help = 'Filename where the learned model weights should be written to (without model definition).')
    arggroup.add_argument('--feature_dump', type = str, default = None, help = 'Filename where learned embeddings for test images should be written to.')
    arggroup.add_argument('--log_dir', type = str, default = None, help = 'Tensorboard log directory.')
    arggroup.add_argument('--no_progress', action = 'store_true', default = False, help = 'Do not display training progress, but just the final performance.')
    arggroup = parser.add_argument_group('Parameters for --lr_schedule=SGD')
    arggroup.add_argument('--sgd_patience', type = int, default = None, help = 'Patience of learning rate reduction in epochs.')
    arggroup.add_argument('--sgd_lr', type = float, default = 0.1, help = 'Initial learning rate.')
    arggroup.add_argument('--sgd_min_lr', type = float, default = None, help = 'Minimum learning rate.')
    arggroup = parser.add_argument_group('Parameters for --lr_schedule=SGDR')
    arggroup.add_argument('--sgdr_base_len', type = int, default = None, help = 'Length of first cycle in epochs.')
    arggroup.add_argument('--sgdr_mul', type = int, default = None, help = 'Multiplier for cycle length after each cycle.')
    arggroup.add_argument('--sgdr_max_lr', type = float, default = None, help = 'Maximum learning rate.')
    arggroup = parser.add_argument_group('Parameters for --lr_schedule=CLR')
    arggroup.add_argument('--clr_step_len', type = int, default = None, help = 'Length of each step in epochs.')
    arggroup.add_argument('--clr_min_lr', type = float, default = None, help = 'Minimum learning rate.')
    arggroup.add_argument('--clr_max_lr', type = float, default = None, help = 'Maximum learning rate.')
    args = parser.parse_args()
    
    if args.val_batch_size is None:
        args.val_batch_size = args.batch_size

    # Configure environment
    K.set_session(K.tf.Session(config = K.tf.ConfigProto(gpu_options = { 'allow_growth' : True })))

    # Load class centroids if given
    centroids = class_list = None
    embed_dim = args.embed_dim
    if args.centroids:
        with open(args.centroids, 'rb') as pf:
            centroids = pickle.load(pf)
            class_list = centroids['ind2label']
            centroids = centroids['embedding']
            embed_dim = centroids.shape[1]
    elif args.class_list is not None:
        with open(args.class_list) as class_file:
            class_list = list(OrderedDict((l.strip().split()[0], None) for l in class_file if l.strip() != '').keys())
            try:
                class_list = [int(lbl) for lbl in class_list]
            except ValueError:
                pass

    # Load dataset
    data_generator = get_data_generator(args.dataset, args.data_root, classes = class_list)

    # Construct and train model
    if (args.gpus <= 1) or args.gpu_merge:
        embed_model = utils.build_network(embed_dim, args.architecture)
        model = center_loss_model(embed_model, centroids if centroids is not None else data_generator.num_classes)
        par_model = model if args.gpus <= 1 else keras.utils.multi_gpu_model(model, gpus = args.gpus, cpu_merge = False)
    else:
        with K.tf.device('/cpu:0'):
            embed_model = utils.build_network(embed_dim, args.architecture)
            model = center_loss_model(embed_model, centroids if centroids is not None else data_generator.num_classes)
        par_model = keras.utils.multi_gpu_model(model, gpus = args.gpus)
    if not args.no_progress:
        model.summary()

    batch_transform_kwargs = { 'num_classes' : data_generator.num_classes }

    # Load pre-trained weights and train last layer for a few epochs
    if args.finetune:
        print('Loading pre-trained weights from {}'.format(args.finetune))
        model.load_weights(args.finetune, by_name=True, skip_mismatch=True)
        print('Pre-training new layers')
        for layer in model.layers:
            layer.trainable = (layer.name in ('embedding', 'embedding_bn', 'prob', 'cls_centroids'))
        embed_model.layers[-1].trainable = True
        par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, momentum=0.9, clipnorm = args.clipgrad),
                          loss = { 'prob' : 'categorical_crossentropy', 'center_loss' : lambda y_true, y_pred: y_pred },
                          loss_weights = { 'prob' : 1.0, 'center_loss' : args.center_loss_weight },
                          metrics = { 'prob' : 'accuracy' })
        par_model.fit_generator(
                data_generator.train_sequence(args.batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
                validation_data = data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
                epochs = args.finetune_init, verbose = not args.no_progress,
                max_queue_size = args.queue_size, workers = args.read_workers, use_multiprocessing = True)
        for layer in model.layers:
            layer.trainable = True
        print('Full model training')
    
    # Train model
    callbacks, num_epochs = utils.get_lr_schedule(args.lr_schedule, data_generator.num_train, args.batch_size, schedule_args = { arg_name : arg_val for arg_name, arg_val in vars(args).items() if arg_val is not None })

    if args.log_dir:
        if os.path.isdir(args.log_dir):
            shutil.rmtree(args.log_dir, ignore_errors = True)
        callbacks.append(keras.callbacks.TensorBoard(log_dir = args.log_dir, write_graph = False))

    if args.max_decay > 0:
        decay = (1.0/args.max_decay - 1) / ((data_generator.num_train // args.batch_size) * (args.epochs if args.epochs else num_epochs))
    else:
        decay = 0.0
    par_model.compile(optimizer = keras.optimizers.SGD(lr=args.sgd_lr, decay=decay, momentum=0.9, clipnorm = args.clipgrad),
                      loss = { 'prob' : 'categorical_crossentropy', 'center_loss' : lambda y_true, y_pred: y_pred },
                      loss_weights = { 'prob' : 1.0, 'center_loss' : args.center_loss_weight },
                      metrics = { 'prob' : 'accuracy' })

    par_model.fit_generator(
              data_generator.train_sequence(args.batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
              validation_data = data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs),
              epochs = args.epochs if args.epochs else num_epochs,
              callbacks = callbacks, verbose = not args.no_progress,
              max_queue_size = args.queue_size, workers = args.read_workers, use_multiprocessing = True)

    # Evaluate final performance
    print(par_model.evaluate_generator(data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs)))
    test_pred = par_model.predict_generator(data_generator.test_sequence(args.val_batch_size, batch_transform = transform_inputs, batch_transform_kwargs = batch_transform_kwargs))[0].argmax(axis=-1)
    class_freq = np.bincount(data_generator.labels_test)
    print('Average Accuracy: {:.4f}'.format(
        ((test_pred == np.asarray(data_generator.labels_test)).astype(np.float) / class_freq[np.asarray(data_generator.labels_test)]).sum() / len(class_freq)
    ))

    # Save model
    if args.weight_dump:
        try:
            model.save_weights(args.weight_dump)
        except Exception as e:
            print('An error occurred while saving the model weights: {}'.format(e))
    if args.model_dump:
        try:
            model.save(args.model_dump)
        except Exception as e:
            print('An error occurred while saving the model: {}'.format(e))

    # Save test image embeddings
    if args.feature_dump:
        pred_features = embed_model.predict_generator(data_generator.flow_test(1, False), data_generator.num_test)
        with open(args.feature_dump,'wb') as dump_file:
            pickle.dump({ 'feat' : dict(enumerate(pred_features)) }, dump_file)
