# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import sys
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

# import torchvision.utils as vutils
from fairseq import utils


from . import (
    FairseqModel, FairseqEncoder, FairseqIncrementalDecoder, 
    register_model, register_model_architecture,
)

from fairseq.modules import (
    ResNetAddUpNoNorm2, ResNetAddUpNoNorm,
    ConvNetActions,
    SinusoidalPositionalEmbedding,
    LearnedPositionalEmbedding,
    HMMControls3, LLControls, FBControls,
    GridMAX,
)


@register_model('attn2d_dynamic_ll')
class Attn2dWaitkModel(FairseqModel):

    def __init__(self, encoder, decoder):
        super().__init__(encoder, decoder)

    def forward(self, target, src_tokens, src_lengths, prev_output_tokens):
        encoder_out = self.encoder(src_tokens, src_lengths)
        decoder_out = self.decoder.forward_train(prev_output_tokens, encoder_out, target)
        return decoder_out

    @staticmethod
    def add_args(parser):
        """ Add model-specific arguments to the parser. """
        """ Embeddings """
        parser.add_argument('--pooling-policy', type=str, default='row',
                            help='Policy for pooling the grid')

        parser.add_argument('--skip-output-mapping', action='store_true',
                            help='remove the final mapping if equal dimension')

        parser.add_argument('--share-all-embeddings', action='store_true',
                            help='share encoder, decoder and output embeddings'
                                 ' (requires shared dictionary and embed dim)')
        parser.add_argument('--share-decoder-input-output-embed', action='store_true',
                            help='share decoder input and output embeddings')
        parser.add_argument('--add-positional-embeddings', default=False, action='store_true',
                            help='if set, enables positional embeddings')
        parser.add_argument('--learned-pos', action='store_true',
                            help='use learned positional embeddings')

        parser.add_argument('--encoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained encoder embedding')
        parser.add_argument('--decoder-embed-path', type=str, metavar='STR',
                            help='path to pre-trained decoder embedding')

        parser.add_argument('--encoder-embed-dim', type=int, metavar='N',
                            help='encoder embedding dimension')
        
        parser.add_argument('--decoder-embed-dim', type=int, metavar='N',
                            help='decoder embedding dimension')
        parser.add_argument('--ffn-dim', type=int, 
                            help='ffn dimension')
        parser.add_argument('--reduce-dim', type=int, 
                            help='first conv output dimension')
        parser.add_argument('--double-masked', action='store_true',
                            help='Mask the future source as well')
        parser.add_argument('--aggregation', type=str, default='max')

        parser.add_argument('--conv-groups', type=int,
                            help='convolution groups')
        parser.add_argument('--source-dilation', default=1, type=int, 
                            help='2nd dimension dilation')
        parser.add_argument('--target-dilation', default=1, type=int, 
                            help='1st dimension dilation')
        parser.add_argument('--conv-stride', default=1, type=int, 
                            help='2nd dimension stride')
        parser.add_argument('--maintain-resolution', default=1, type=int, 
                            help='pad so that the output dimension matches the input')
        parser.add_argument('--output-dim', type=int, 
                            help='pre-softmax output dimension')
        parser.add_argument('--num-heads', type=int)
        parser.add_argument('--conv-bias', action='store_true')
        parser.add_argument('--embeddings-ln', action='store_true',
                            help='add LN after the embeddings')
        parser.add_argument('--network', type=str, metavar='STR',
                            help='Type of cnv net between denseNet or resNet')

        parser.add_argument('--blocks', type=str, metavar='STR',
                            help='specific architecture that overwrites the kernel, growth...')
        parser.add_argument('--kernel-size', type=int, help='kernel size')
        parser.add_argument('--bn-size', type=int, 
                            help='bn size in the dense layer')
        parser.add_argument('--growth-rate', type=int, 
                            help='growth rate')
        parser.add_argument('--num-layers', type=int, help='number of layers')

        parser.add_argument('--convolution-dropout', type=float, metavar='D',
                            help='dropout probability in the conv layers')

        parser.add_argument('--input-dropout', type=float, metavar='D',
                            help='dropout probability on the initial 2d input')
        parser.add_argument('--embeddings-dropout', type=float, metavar='D',
                            help='dropout probability on the embeddings')

        parser.add_argument('--prediction-dropout', type=float, metavar='D',
                            help='dropout on the final prediction layer')
        parser.add_argument('--init-weights', type=str, metavar='STR',
                            help='the type of weight initialiation')
        parser.add_argument('--divide-channels', type=int, metavar='INT',
                            help='the factor to reduce the input channels by')
        parser.add_argument('--memory-efficient', action='store_true',
                            help='use checkpointing')
        parser.add_argument('--nonzero-padding', action='store_true',
                            help='Do not zero out padding positions in the conv activations')

        # Controller
        parser.add_argument('--control-kernel-size', type=int, help='kernel size')
        parser.add_argument('--num-control-layers', type=int, help='number of layers')
        parser.add_argument('--detach-controls', action='store_true')
        parser.add_argument('--oracle-penalty', type=float, default=0)
        parser.add_argument('--write-right', action='store_true')
        parser.add_argument('--control-oracle', type=str, default='likelihood')


    def log_tensorboard(self, writer, iter):
        pass

    @classmethod
    def build_model(cls, args, task):
        """ Build a new model instance. """
        base_architecture(args)

        if not hasattr(args, 'max_source_positions'):
            args.max_source_positions = 1024
        if not hasattr(args, 'max_target_positions'):
            args.max_target_positions = 1024

        src_dict, tgt_dict = task.source_dictionary, task.target_dictionary

        def build_embedding(dictionary, embed_dim, path=None):
            num_embeddings = len(dictionary)
            padding_idx = dictionary.pad()
            emb = Embedding(num_embeddings, embed_dim, padding_idx)
            # if provided, load from preloaded dictionaries
            if path:
                embed_dict = utils.parse_embedding(path)
                utils.load_embedding(embed_dict, dictionary, emb)
            return emb

        if args.share_all_embeddings:
            if src_dict != tgt_dict:
                raise RuntimeError('--share-all-embeddings requires a joined dictionary')
            if args.encoder_embed_dim != args.decoder_embed_dim:
                raise RuntimeError(
                    '--share-all-embeddings requires --encoder-embed-dim to match --decoder-embed-dim')
            if args.decoder_embed_path and (
                    args.decoder_embed_path != args.encoder_embed_path):
                raise RuntimeError('--share-all-embeddings not compatible with --decoder-embed-path')
            encoder_embed_tokens = build_embedding(
                src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = encoder_embed_tokens
            args.share_decoder_input_output_embed = True
        else:
            encoder_embed_tokens = build_embedding(
                src_dict, args.encoder_embed_dim, args.encoder_embed_path
            )
            decoder_embed_tokens = build_embedding(
                tgt_dict, args.decoder_embed_dim, args.decoder_embed_path
            )

        encoder = Attn2dEncoder(args, src_dict, encoder_embed_tokens)
        decoder = Attn2dDecoder(args, tgt_dict, decoder_embed_tokens)

        return cls(encoder, decoder)

    def max_decoder_positions(self):
        """ Maximum input length supported by the decoder """
        return self.decoder.max_target_positions 


class Attn2dEncoder(FairseqEncoder):
    def __init__(self, args,  dictionary, embed_tokens, left_pad=False):
        super().__init__(dictionary)
        embed_dim = embed_tokens.embedding_dim
        self.padding_idx = embed_tokens.padding_idx
        self.max_source_positions = args.max_source_positions
        
        self.embed_tokens = embed_tokens
        self.embed_scale = math.sqrt(embed_dim)
        self.embed_positions = PositionalEmbedding(
            self.max_source_positions, 
            embed_dim, self.padding_idx,
            left_pad=args.left_pad_source,
            learned=args.learned_pos,
        ) if args.add_positional_embeddings else None

        self.embedding_dropout = nn.Dropout(args.embeddings_dropout)
        
    def forward(self, src_tokens, src_lengths=None, **kwargs):
        x = self.embed_scale * self.embed_tokens(src_tokens)
        if self.embed_positions is not None:
            x += self.embed_positions(src_tokens)
        x = self.embedding_dropout(x)
        encoder_padding_mask = src_tokens.eq(self.padding_idx)
        if not encoder_padding_mask.any():
            encoder_padding_mask = None

        return {
            'encoder_out': x, # B, Ts, C
            'encoder_padding_mask': encoder_padding_mask  # B, Ts
        }

    def max_positions(self):
        """Maximum input length supported by the encoder."""
        if self.embed_positions is None:
            return self.max_source_positions
        return min(self.max_source_positions, self.embed_positions.max_positions())

    def reorder_encoder_out(self, encoder_out, new_order):
        """
        Reorder encoder output according to *new_order*.

        Args:
            encoder_out: output from the ``forward()`` method
            new_order (LongTensor): desired order

        Returns:
            *encoder_out* rearranged according to *new_order*
        """

        if encoder_out['encoder_out'] is not None:
            encoder_out['encoder_out'] = \
                encoder_out['encoder_out'].index_select(0, new_order)
        if encoder_out['encoder_padding_mask'] is not None:
            encoder_out['encoder_padding_mask'] = \
                encoder_out['encoder_padding_mask'].index_select(0, new_order)
        return encoder_out


class Attn2dDecoder(FairseqIncrementalDecoder):
    """ Pervasive Attention Model """

    def __init__(self, args,  dictionary, embed_tokens, left_pad=False):
        super().__init__(dictionary)
        self.share_input_output_embed = args.share_decoder_input_output_embed

        self.decoder_dim = args.decoder_embed_dim
        embed_dim = embed_tokens.embedding_dim
        self.padding_idx = embed_tokens.padding_idx
        self.max_target_positions = args.max_target_positions

        self.embed_tokens = embed_tokens
        self.embed_scale = math.sqrt(embed_dim)
        self.embed_positions = PositionalEmbedding(
            args.max_target_positions, 
            embed_dim, self.padding_idx,
            left_pad=args.left_pad_target,
            learned=args.learned_pos,
        ) if args.add_positional_embeddings else None

        self.embedding_dropout = nn.Dropout(args.embeddings_dropout)
        self.input_dropout = nn.Dropout(args.input_dropout)
        self.input_channels = args.encoder_embed_dim + args.decoder_embed_dim
        self.output_dim = args.output_dim

        print('Input channels:', self.input_channels)
        if args.network == 'resnet_addup_nonorm2':
            self.net = ResNetAddUpNoNorm2(self.input_channels, args)
        elif args.network == 'resnet_addup_nonorm':
            self.net = ResNetAddUpNoNorm(self.input_channels, args)

        self.output_channels = self.net.output_channels
        
        self.aggregator = GridMAX(self.output_channels)
        
        print('Decoder dim:', self.decoder_dim)
        print('The ConvNet output channels:', self.output_channels)
        print('Required output dim:', self.output_dim)

        if not self.output_dim == self.output_channels or not args.skip_output_mapping:
            self.projection = Linear(
                self.output_channels,
                self.output_dim,
                dropout=args.prediction_dropout
            )

        else:
            self.projection = None
        self.prediction_dropout = nn.Dropout(args.prediction_dropout)
        if self.share_input_output_embed:
            self.prediction = Linear(
                self.decoder_dim,
                len(dictionary)
            )
            self.prediction.weight = self.embed_tokens.weight
        else:
            self.prediction = Linear(
                self.output_dim,
                len(dictionary)
            )

        # Controller:
        controller_dim = args.encoder_embed_dim + args.decoder_embed_dim
        self.controller_feat = ConvNetActions(controller_dim,
                                              args.num_control_layers,
                                              args.control_kernel_size)

        if args.control_oracle == 'likelihood':
            self.controller = LLControls(
                    args,
                    controller_dim
                )
        elif args.control_oracle == 'program':
            self.controller = FBControls(
                args,
                controller_dim
            )

        self.detach_controls = args.detach_controls

    def upgrade_state_dict(self, state_dict):
        current_state = self.state_dict()
        keys = list(state_dict)
        for k in keys:
            if 'mconv2.mask' in k:
                state_dict[k] = current_state[k.replace('decoder.', '')]
        return state_dict

    def forward(self, prev_output_tokens, encoder_out,
                incremental_state=None,
                context_size=None,
                cache_decoder=True, **kwargs):
        encoder_states = encoder_out['encoder_out']
        # source embeddings
        Ts = encoder_states.size(1)
        if context_size is not None:
            src_emb = encoder_states[:, :context_size]
        # target embeddings:
        positions = self.embed_positions(
            prev_output_tokens,
            incremental_state=incremental_state if cache_decoder else None,
        ) if self.embed_positions is not None else None

        if incremental_state is not None and cache_decoder:
            # embed the last target token
            prev_output_tokens = prev_output_tokens[:, -1:]
            if positions is not None:
                positions = positions[:, -1:]
            
        decoder_mask = prev_output_tokens.eq(self.padding_idx)
        if not decoder_mask.any():
            decoder_mask = None

        # Build the full grid
        tgt_emb = self.embed_scale * self.embed_tokens(prev_output_tokens)
        if positions is not None:
            tgt_emb += positions

        tgt_emb = self.embedding_dropout(tgt_emb)
                
        src_length = src_emb.size(1)
        tgt_length = tgt_emb.size(1)

        # build 2d "image" of embeddings
        src_emb = _expand(src_emb, 1, tgt_length)  # B, Tt, Ts, ds
        tgt_emb = _expand(tgt_emb, 2, src_length)  # B, Tt, Ts, dt
        x = torch.cat((src_emb, tgt_emb), dim=3)   # B, Tt, Ts, C=ds+dt
        x = self.input_dropout(x)
        # pass through dense convolutional layers
        encoder_mask = encoder_out['encoder_padding_mask']
        x = self.net(
            x, 
            decoder_mask=decoder_mask,
            encoder_mask=encoder_mask,
            incremental_state=incremental_state if cache_decoder else None
        )  # B, Tt, Ts, C

        if incremental_state is not None:
            # Keep only the last step:
            x = x[:, -1:]
        
        # x = self.aggregator(x)
        x, _ = x.max(dim=2)  # B, Tt, C
        x = self.projection(x) if self.projection is not None else x  # B, Tt, C
        x = self.prediction_dropout(x)

        # multiply by embedding matrix to generate distribution
        x = self.prediction(x)  # B, Tt, V
        return x, None

    def decide(self, prev_output_tokens, encoder_out, context_size):
        torch.set_printoptions(precision=2)
        # source embeddings
        src_emb = encoder_out['encoder_out'][:, :context_size]  # B, Ts, ds 
        # target embeddings:
        positions = self.embed_positions(
            prev_output_tokens,
            incremental_state=None,
        ) if self.embed_positions is not None else None
        # Build the full grid
        tgt_emb = self.embed_scale * self.embed_tokens(prev_output_tokens)
        if positions is not None:
            tgt_emb += positions
        tgt_emb = self.embedding_dropout(tgt_emb)
        src_length = src_emb.size(1)
        tgt_length = tgt_emb.size(1)
        # build 2d "image" of embeddings
        src_emb = _expand(src_emb, 1, tgt_length)  # B, Tt, Ts, ds
        tgt_emb = _expand(tgt_emb, 2, src_length)  # B, Tt, Ts, dt
        x = torch.cat((src_emb, tgt_emb), dim=3)   # B, Tt, Ts, C=ds+dt
        obs = self.controller_feat(x)
        controls = self.controller.predict_read_write(obs) 
        pwrite = torch.exp(controls[:,-1,-1,1])
        return pwrite 

    def forward_train(self, prev_output_tokens, encoder_out, target, **kwargs):
        torch.set_printoptions(precision=2)
        # source embeddings
        src_emb = encoder_out['encoder_out']  # B, Ts, ds 
        # target embeddings:
        positions = self.embed_positions(
            prev_output_tokens,
            incremental_state=None,
        ) if self.embed_positions is not None else None

        decoder_mask = prev_output_tokens.eq(self.padding_idx)
        if not decoder_mask.any():
            decoder_mask = None

        # Build the full grid
        tgt_emb = self.embed_scale * self.embed_tokens(prev_output_tokens)
        if positions is not None:
            tgt_emb += positions
        tgt_emb = self.embedding_dropout(tgt_emb)
        batch_size = src_emb.size(0)
        src_length = src_emb.size(1)
        tgt_length = tgt_emb.size(1)

        # build 2d "image" of embeddings
        src_emb = _expand(src_emb, 1, tgt_length)  # B, Tt, Ts, ds
        tgt_emb = _expand(tgt_emb, 2, src_length)  # B, Tt, Ts, dt
        x = torch.cat((src_emb, tgt_emb), dim=3)   # B, Tt, Ts, C=ds+dt
        x = self.input_dropout(x)

        if self.detach_controls:
            observations = self.controller_feat(x.clone().detach())
        else:
            observations = self.controller_feat(x)
        # pass through dense convolutional layers
        encoder_mask = encoder_out['encoder_padding_mask']
        x = self.net(
            x, 
            decoder_mask=decoder_mask,
            encoder_mask=encoder_mask,
            incremental_state=None,
        )  # B, Tt, Ts, C
        x, _ = self.aggregator(x)  # B, Tt, Ts, C
        x = self.projection(x) if self.projection is not None else x  # B, Tt, C

        # Predict
        x = self.prediction_dropout(x)
        x = self.prediction(x)  # B, Tt, Ts, V
        x = utils.log_softmax(x, dim=-1)

        with torch.no_grad():
            # Gather log p(ground truth)
            scores = x.view(-1, x.size(-1)).gather(
                dim=-1,
                index=target.unsqueeze(-1).expand(-1, -1, src_length).contiguous().view(-1, 1)
            ).view(batch_size, tgt_length, src_length)
            # Forbid padding positions:
            if encoder_mask is not None:
                scores = scores.masked_fill(encoder_mask.unsqueeze(1), -1000)
            if decoder_mask is not None:
                scores = scores.masked_fill(decoder_mask.unsqueeze(-1), -1000)

        controls, gamma, read_labels, write_labels = self.controller(observations, scores)
        return x, observations, controls, gamma, read_labels, write_labels

        
@register_model_architecture('attn2d_dynamic_ll', 'attn2d_dynamic_ll')
def base_architecture(args):
    args.memory_efficient = getattr(args, 'memory_efficient', False)
    args.nonzero_padding = getattr(args, 'nonzero_padding', False)

    args.conv_bias = getattr(args, 'conv_bias', False)
    args.aggregation = getattr(args, 'aggregation', 'max')

    args.skip_output_mapping = getattr(args, 'skip_output_mapping', False)

    args.encoder_embed_path = getattr(args, 'encoder_embed_path', None)
    args.decoder_embed_path = getattr(args, 'decoder_embed_path', None)
    args.share_decoder_input_output_embed = getattr(
        args, 'share_decoder_input_output_embed', False
    )
    args.share_all_embeddings = getattr(args, 'share_all_embeddings', False)
    args.embeddings_dropout = getattr(args, 'embeddings_dropout', 0.)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 256)
    args.decoder_embed_dim = getattr(args, 'decoder_embed_dim', 256)
    args.ffn_dim = getattr(args, 'ffn_dim', 512)
    args.output_dim = getattr(args, 'output_dim', args.decoder_embed_dim)
    args.divide_channels = getattr(args, 'divide_channels', 2)
    args.reduce_dim = getattr(args, 'reduce_dim',
                              (args.encoder_embed_dim + args.decoder_embed_dim) // args.divide_channels)
    args.conv_groups = getattr(args, 'conv_groups', args.reduce_dim)
    args.num_heads = getattr(args, 'num_heads', 1)
    args.conv_groups = getattr(args, 'conv_groups', 1)

    args.conv_stride = getattr(args, 'conv_stride', 1)
    args.source_dilation = getattr(args, 'source_dilation', 1)
    args.target_dilation = getattr(args, 'target_dilation', 1)
    args.maintain_resolution = getattr(args, 'maintain_resolution', 1)
    
    args.add_positional_emnbeddings = getattr(args, 'add_positional_embeddings', False)
    args.learned_pos = getattr(args, 'learned_pos', False)

    args.input_dropout = getattr(args, 'input_dropout', 0.2)
    args.convolution_dropout = getattr(args, 'convolution_dropout', 0.2)
    args.network = getattr(args, 'network', 'resnet_addup_nonorm2')
    args.kernel_size = getattr(args, 'kernel_size', 3)
    args.num_layers = getattr(args, 'num_layers', 24)

    args.prediction_dropout = getattr(args, 'prediction_dropout', 0.2)
    args.double_masked = getattr(args, 'double_masked', True)
    
    # Controller
    args.detach_controls = getattr(args, 'detach_controls', False)
    args.write_right = getattr(args, 'write_right', False)
    args.oracle_penalty = getattr(args, 'oracle_penalty', 0)
    args.control_oracle = getattr(args, 'control_oracle', 'likelihood')
    args.num_control_layers = getattr(args, 'num_control_layers', 8)
    args.control_kernel_size = getattr(args, 'control_kernel_size', 3)


def _expand(tensor, dim, reps):
    tensor = tensor.unsqueeze(dim)
    shape = tuple(reps if i == dim else -1 for i in range(tensor.dim()))
    return tensor.expand(shape)


def PositionalEmbedding(num_embeddings, embedding_dim,
                        padding_idx, left_pad, learned=False):
    if learned:
        m = LearnedPositionalEmbedding(num_embeddings + padding_idx + 1,
                                       embedding_dim, padding_idx, left_pad)
        nn.init.normal_(m.weight, mean=0, std=embedding_dim ** -0.5)
        nn.init.constant_(m.weight[padding_idx], 0)
    else:
        m = SinusoidalPositionalEmbedding(embedding_dim, padding_idx, left_pad,
                                          num_embeddings + padding_idx + 1)
    return m


def Embedding(num_embeddings, embedding_dim, padding_idx):
    m = nn.Embedding(num_embeddings, embedding_dim, padding_idx=padding_idx)
    nn.init.normal_(m.weight, mean=0, std=embedding_dim ** -0.5)
    nn.init.constant_(m.weight[padding_idx], 0)
    return m


def Linear(in_features, out_features, dropout=0., bias=True):
    m = nn.Linear(in_features, out_features, bias=bias)
    nn.init.normal_(m.weight, mean=0,
                    std=math.sqrt((1 - dropout) / in_features))
    nn.init.constant_(m.bias, 0)
    return m



