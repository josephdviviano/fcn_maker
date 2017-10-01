from keras.models import Model
from keras.layers import (Input,
                          Activation,
                          Dense,
                          Permute,
                          Lambda,
                          add,
                          concatenate)
from keras.layers.normalization import BatchNormalization
from keras import backend as K
from theano import tensor as T
from keras.regularizers import l2
import numpy as np
from .blocks import (bottleneck,
                     basic_block,
                     basic_block_mp,
                     residual_block,
                     Convolution,
                     get_nonlinearity)


def _l2(decay):
    """
    Return a new instance of l2 regularizer, or return None
    """
    if decay is not None:
        return l2(decay)
    else:
        return None
    

def _softmax(x):
    """
    Softmax that works on ND inputs.
    """
    e = K.exp(x - K.max(x, axis=-1, keepdims=True))
    s = K.sum(e, axis=-1, keepdims=True)
    return e / s
    
    
def assemble_model(input_shape, num_classes, num_adapt_blocks, num_main_blocks,
                   main_block_depth, num_filters, short_skip=True,
                   long_skip=True, long_skip_merge_mode='concat',
                   mainblock=None, initblock=None, skipblock=None,
                   skipblock_num_filters=None, num_residuals=1, dropout=0.,
                   normalization=BatchNormalization, norm_kwargs=None,
                   weight_decay=None, init='he_normal', batch_norm=True, 
                   nonlinearity='relu', ndim=2, verbose=True):
    """
    input_shape : A tuple specifiying the 2D image input shape.
    num_classes : The number of classes in the segmentation output.
    num_adapt_blocks : The number of blocks of type initblock, above 
        mainblocks. These blocks always have the same number of channels as
        the first convolutional layer in the model.
    num_main_blocks : The number of blocks of type mainblock, below initblocks.
        These blocks double (halve) the number of channels at each downsampling
        (upsampling).
    main_block_depth : An integer or list of integers specifying the number of
        repetitions of each mainblock. A list must contain 2*num_main_blocks+1
        values (there are num_mainblocks on the contracting path and on the 
        expanding path, as well as as one on the across path). Zero is a valid
        depth.
    num_filters : Can be an int or a list of ints, specifying the number of
        filters for each block.
        If an int, sets the number filters in the first and last convolutional
        layer in the model, as well as of every adapt_block. Each main_block
        doubles (halves) the number of filters for each decrease (increase) in
        resolution.
        If a list, specifies the number of filters for each convolution/block.
        Must be of length 2*(num_main_blocks+num_adapt_blocks)+3.
    short_skip : A boolean specifying whether to use ResNet-like shortcut
        connections from the input of each block to its output. The inputs are
        summed with the outputs.
    long_skip : A boolean specifying whether to use UNet-like skip connections
        from the downward path to the upward path. These can either concatenate
        or sum features across.
    long_skip_merge_mode : Either 'concat' or 'sum' features across long_skip.
    mainblock : A layer defining the mainblock (bottleneck by default).
    initblock : A layer defining the initblock (basic_block_mp by default).
    skipblock_num_filters : The number of filters to use for skip blocks.
        If None, skip blocks will not be used..
    skipblock : A layer defining the skipblock (basic_block_mp by default).
    num_residuals : The number of parallel residual functions per block.
    dropout : A float [0, 1] specifying the dropout probability, introduced in
        every block.
    normalization : the normalization to apply to layers (by default: batch
        normalization). If None, no normalization is applied.
    norm_kwargs : keyword arguments to pass to batch norm layers. For batch
        normalization, default momentum is 0.9.
    weight_decay : The weight decay (L2 penalty) used in every convolution 
        (float).
    init : A string specifying (or a function defining) the initializer for
        layers.
    num_outputs : The number of model outputs, each with num_classifier
        classifiers.
    ndim : The spatial dimensionality of the input and output (either 2 or 3).
    verbose : A boolean specifying whether to print messages about model   
        structure during construction (if True).
    """
    
    '''
    By default, use depth 2 bottleneck for mainblock
    '''
    if mainblock is None:
        mainblock = bottleneck
    if initblock is None:
        initblock = basic_block_mp
    if skipblock is None:
        skipblock = basic_block_mp
    
    '''
    main_block_depth can be a list per block or a single value 
    -- ensure the list length is correct (if list) or convert to list
    '''
    if hasattr(main_block_depth, '__len__'):
        if len(main_block_depth)!=2*num_main_blocks+1:
            raise ValueError("main_block_depth must have " 
                             "`2*num_main_blocks+1` values when " 
                             "passed as a list")
    else:
        main_block_depth = [main_block_depth]*(2*num_main_blocks+1)
    
    '''
    num_filters can be a list per convolution/block or a single value
    -- ensure the list length is correct (if list) or convert to list
    '''
    if hasattr(num_filters, '__len__'):
        if len(num_filters)!=2*(num_main_blocks+num_adapt_blocks)+3:
            raise ValueError("num_filters must have "
                             "`2*(num_main_blocks+num_adapt_blocks)+3` values "
                             "when passed as a list")
    else:
        num_filters_list = [num_filters]*(num_adapt_blocks+1)
        num_filters_list += [num_filters*(2**b) \
                                             for b in range(num_main_blocks+1)]
        num_filters_list += num_filters_list[-2::-1]
        num_filters = num_filters_list
        
    '''
    ndim must be only 2 or 3.
    '''
    if ndim not in [2, 3]:
        raise ValueError("ndim must be either 2 or 3")
            
    '''
    If BatchNormalization is used and norm_kwargs is not set, set default
    kwargs.
    '''
    if norm_kwargs is None:
        if normalization == BatchNormalization:
            norm_kwargs = {'momentum': 0.9,
                           'scale': True,
                           'center': True,
                           'axis': 1}
        else:
            norm_kwargs = {}
            
    '''
    Constant kwargs passed to the init and main blocks.
    '''
    block_kwargs = {'skip': short_skip,
                    'dropout': dropout,
                    'weight_decay': weight_decay,
                    'num_residuals': num_residuals,
                    'normalization': normalization,
                    'norm_kwargs': norm_kwargs,
                    'nonlinearity': nonlinearity,
                    'init': init,
                    'ndim': ndim}
    
    '''
    Function to print if verbose==True
    '''
    def v_print(*args, **kwargs):
        if verbose:
            print(*args, **kwargs)
        else:
            return None
    
    '''
    Helper function to create a long skip connection with concatenation.
    Concatenated information is not transformed if use_skip_blocks is False.
    '''
    def make_long_skip(prev_x, concat_x, num_target_filters, name=None):
            
        if skipblock_num_filters is not None:
            skip_kwargs = {}
            skip_kwargs.update(block_kwargs)
            skip_kwargs['repetitions'] = 1
            concat_x = residual_block(skipblock,
                                      filters=skipblock_num_filters,
                                      **skip_kwargs)(concat_x)
        if long_skip_merge_mode == 'sum':
            if prev_x._keras_shape[1] != num_target_filters:
                prev_x = Convolution(filters=num_target_filters,
                                     kernel_size=1,
                                     ndim=ndim,
                                     kernel_initializer=init,
                                     padding='valid',
                                     kernel_regularizer=_l2(weight_decay),
                                     name=name+'_prev')(prev_x)
            if concat_x._keras_shape[1] != num_target_filters:
                concat_x = Convolution(filters=num_target_filters,
                                       kernel_size=1,
                                       ndim=ndim,
                                       kernel_initializer=init,
                                       padding='valid',
                                       kernel_regularizer=_l2(weight_decay),
                                       name=name+'_concat')(concat_x)
                
        #def _pad_to_fit(x, target_shape):
            #"""
            #Spatially pad a tensor's feature maps with zeros as evenly as
            #possible (center it) to fit the target shape.
            
            #Expected target shape is larger than the shape of the tensor.
            
            #NOTE: padding may be unequal on either side of the map if the
            #target dimension is odd. This is why keras's ZeroPadding2D isn't
            #used.
            #"""
            #pad_0 = {}
            #pad_1 = {}
            #for dim in [2, 3]:
                #pad_0[dim] = (target_shape[dim]-x.shape[dim])//2
                #pad_1[dim] = target_shape[dim]-x.shape[dim]-pad_0[dim]
            #output = T.zeros(target_shape)
            #indices = (slice(None),
                    #slice(None),
                    #slice(pad_0[2], target_shape[2]-pad_1[2]),
                    #slice(pad_0[3], target_shape[3]-pad_1[3]))
            #return T.set_subtensor(output[indices], x)
        #zero_pad = Lambda(_pad_to_fit,
                          #output_shape=concat_x._keras_shape[1:],
                          #arguments={'target_shape': concat_x.shape})
        #prev_x = zero_pad(prev_x)
        
        if long_skip_merge_mode=='sum':
            merged = add([prev_x, concat_x])
        elif long_skip_merge_mode=='concat':
            merged = concatenate([prev_x, concat_x], axis=1)
        else:
            raise ValueError("Unrecognized merge mode: {}"
                             "".format(merge_mode))
        return merged
    
    '''
    Build all the blocks on the contracting and expanding paths.
    '''
    tensors = {}
    model_input = Input(shape=input_shape)
    
    # Initial convolution
    x = Convolution(filters=num_filters[0],
                    kernel_size=3,
                    ndim=ndim,
                    kernel_initializer=init,
                    padding='same',
                    kernel_regularizer=_l2(weight_decay),
                    name='first_conv')(model_input)
    tensors[0] = x
    
    # DOWN (initial subsampling blocks)
    for b in range(0, num_adapt_blocks):
        depth = b+1
        n_filters = num_filters[1+b]
        x = residual_block(initblock,
                           filters=n_filters,
                           repetitions=1,
                           subsample=True,
                           name='initblock_d'+str(b),
                           **block_kwargs)(x)
        tensors[depth] = x
        v_print("ADAPT DOWN {}: {}".format(b, x._keras_shape))
    
    # DOWN (main blocks)
    for b in range(0, num_main_blocks):
        depth = b+1+num_adapt_blocks
        n_filters = num_filters[1+num_adapt_blocks+b]
        x = residual_block(mainblock,
                           filters=n_filters,
                           repetitions=main_block_depth[b],
                           subsample=True,
                           name='mainblock_d'+str(b),
                           **block_kwargs)(x)
        tensors[depth] = x
        if main_block_depth[b]!=0:
            v_print("MAIN DOWN {} (depth {}): {}"
                    "".format(b, main_block_depth[b], x._keras_shape))
        
    # ACROSS
    n_filters = num_filters[1+num_adapt_blocks+num_main_blocks]
    x = residual_block(mainblock,
                       filters=n_filters,
                       repetitions=main_block_depth[num_main_blocks],
                       subsample=True,
                       upsample=True,
                       name='mainblock_a',
                       **block_kwargs)(x) 
    if main_block_depth[num_main_blocks]!=0:
        v_print("ACROSS (depth {}): {}"
                "".format(main_block_depth[num_main_blocks], x._keras_shape))

    # UP (main blocks)
    for b in range(num_main_blocks-1, -1, -1):
        depth = b+1+num_adapt_blocks
        n_filters = num_filters[-1-1-num_adapt_blocks-b]
        if long_skip:
            x = make_long_skip(prev_x=x,
                               concat_x=tensors[depth],
                               num_target_filters=n_filters,
                               name='concat_main_'+str(b))
        x = residual_block(mainblock,
                           filters=n_filters,
                           repetitions=main_block_depth[-b-1],
                           upsample=True,
                           name='mainblock_u'+str(b),
                           **block_kwargs)(x)
        if main_block_depth[-b-1]!=0:
            v_print("MAIN UP {} (depth {}): {}"
                    "".format(b, main_block_depth[-b-1], x._keras_shape))
        
    # UP (final upsampling blocks)
    for b in range(num_adapt_blocks-1, -1, -1):
        depth = b+1
        n_filters = num_filters[-1-1-b]
        if long_skip:
            x = make_long_skip(prev_x=x,
                               concat_x=tensors[depth],
                               num_target_filters=n_filters,
                               name='concat_init_'+str(b))
        x = residual_block(initblock,
                           filters=n_filters,
                           repetitions=1,
                           upsample=True,
                           name='initblock_u'+str(b),
                           **block_kwargs)(x)
        v_print("ADAPT UP {}: {}".format(b, x._keras_shape))
        
    # Final convolution
    if long_skip:
        x = make_long_skip(prev_x=x,
                           concat_x=tensors[0],
                           num_target_filters=num_filters[-1],
                           name='concat_top')
    x = Convolution(filters=num_filters[-1],
                    kernel_size=3,
                    ndim=ndim,
                    kernel_initializer=init,
                    padding='same',
                    kernel_regularizer=_l2(weight_decay),
                    name='final_conv')(x)
    
    if normalization is not None:
        x = normalization(**norm_kwargs)(x)
    x = get_nonlinearity(nonlinearity)(x)
    
    # OUTPUT (SOFTMAX)
    if num_classes is not None:
        all_outputs = []
        for i in range(num_outputs):
            # Linear classifier
            output = Convolution(filters=num_classes,
                                 kernel_size=1,
                                 ndim=ndim
                                 activation='linear',
                                 kernel_regularizer=_l2(weight_decay),
                                 name='logit_conv')(x)
            output = Permute((2,3,1))(output)
            if num_classes==1:
                output = Activation('sigmoid')(output)
            else:
                output = Activation(_softmax)(output)
            output = Permute((3,1,2))(output)
            all_outputs.append(output)
    else:
        # No classifier
        all_outputs = x
    
    # MODEL
    model = Model(inputs=model_input, outputs=output)

    return model
