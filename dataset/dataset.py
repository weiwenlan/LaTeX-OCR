import albumentations as alb
from albumentations.pytorch import ToTensorV2
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
import torch.utils.data as data
from torchvision import transforms
import numpy as np
import imagesize
import logging
import glob
import os
from os.path import join
from collections import defaultdict
import pickle
from PIL import Image
import cv2
from transformers import PreTrainedTokenizerFast
from tqdm.auto import tqdm


train_transform = alb.Compose(
    [
        alb.Compose(
            [alb.ShiftScaleRotate(shift_limit=0, scale_limit=(-.15, 0), rotate_limit=1, border_mode=0, interpolation=3,
                                  value=[255, 255, 255], p=1),
             alb.GridDistortion(distort_limit=0.1, border_mode=0, interpolation=3, value=[255, 255, 255], p=.5)], p=.15),
        alb.InvertImg(p=.15),
        alb.RGBShift(r_shift_limit=15, g_shift_limit=15,
                     b_shift_limit=15, p=0.3),
        alb.GaussNoise(10, p=.2),
        alb.RandomBrightnessContrast(.05, (-.2, 0), True, p=0.2),
        alb.JpegCompression(95, p=.5),
        alb.ToGray(always_apply=True),
        alb.Normalize((0.7931, 0.7931, 0.7931), (0.1738, 0.1738, 0.1738)),
        # alb.Sharpen()
        ToTensorV2(),
    ]
)
test_transform = alb.Compose(
    [
        alb.ToGray(always_apply=True),
        alb.Normalize((0.7931, 0.7931, 0.7931), (0.1738, 0.1738, 0.1738)),
        # alb.Sharpen()
        ToTensorV2(),
    ]
)


class Im2LatexDataset:
    keep_smaller_batches = False
    shuffle = True
    batchsize = 16
    max_dimensions = (1024, 512)
    pad_token = "[PAD]"
    bos_token = "[BOS]"
    eos_token = "[EOS]"
    pad_token_id = 0
    bos_token_id = 1
    eos_token_id = 2
    transform = train_transform

    def __init__(self, equations=None, images=None, tokenizer=None, shuffle=True, batchsize=16, max_dimensions=(1024, 512), pad=False, keep_smaller_batches=False, test=False):
        """Generates a torch dataset from pairs of `equations` and `images`.

        Args:
            equations (str, optional): Path to equations. Defaults to None.
            images (str, optional): Directory where images are saved. Defaults to None.
            tokenizer (str, optional): Path to saved tokenizer. Defaults to None.
            shuffle (bool, opitonal): Defaults to True. 
            batchsize (int, optional): Defaults to 16.
            max_dimensions (tuple(int, int), optional): Maximal dimensions the model can handle
            pad (bool): Pad the images to `max_dimensions`. Defaults to False.
            keep_smaller_batches (bool): Whether to also return batches with smaller size than `batchsize`. Defaults to False.
            test (bool): Whether to use the test transformation or not. Defaults to False.
        """

        if images is not None and equations is not None:
            assert tokenizer is not None
            self.images = [path.replace('\\', '/') for path in glob.glob(join(images, '*.png'))]
            self.sample_size = len(self.images)
            eqs = open(equations, 'r').read().split('\n')
            self.indices = [int(os.path.basename(img).split('.')[0]) for img in self.images]
            self.tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer)
            self.shuffle = shuffle
            self.batchsize = batchsize
            self.max_dimensions = max_dimensions
            self.pad = pad
            self.keep_smaller_batches = keep_smaller_batches
            self.test = test
            self.data = defaultdict(lambda: [])
            # check the image dimension for every image and group them together
            try:
                for i, im in tqdm(enumerate(self.images), total=len(self.images)):
                    width, height = imagesize.get(im)
                    if width <= max_dimensions[0] and height <= max_dimensions[1]:
                        self.data[(width, height)].append((eqs[self.indices[i]], im))
            except KeyboardInterrupt:
                pass
            self.data = dict(self.data)
            self._get_size()

            iter(self)

    def __len__(self):
        return self.size

    def __iter__(self):
        self.i = 0
        self.transform = test_transform if self.test else train_transform
        self.pairs = []
        for k in self.data:
            info = np.array(self.data[k], dtype=object)
            p = torch.randperm(len(info)) if self.shuffle else torch.arange(len(info))
            for i in range(0, len(info), self.batchsize):
                batch = info[p[i:i+self.batchsize]]
                if len(batch.shape) == 1:
                    batch = batch[None, :]
                if len(batch) < self.batchsize and not self.keep_smaller_batches:
                    continue
                self.pairs.append(batch)
        if self.shuffle:
            self.pairs = np.random.permutation(np.array(self.pairs, dtype=object))
        else:
            self.pairs = np.array(self.pairs, dtype=object)
        self.size = len(self.pairs)
        return self

    def __next__(self):
        if self.i >= self.size:
            raise StopIteration
        self.i += 1
        return self.prepare_data(self.pairs[self.i-1])

    def prepare_data(self, batch):
        """loads images into memory

        Args:
            batch (numpy.array[[str, str]]): array of equations and image path pairs

        Returns:
            tuple(torch.tensor, torch.tensor): data in memory
        """

        eqs, ims = batch.T
        images = []
        for path in list(ims):
            im = cv2.imread(path)
            if im is None:
                print(path, 'not found!')
                continue
            im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
            images.append(self.transform(image=im)['image'][:1])
        tok = self.tokenizer(list(eqs), return_token_type_ids=False)
        # pad with bos and eos token
        for k, p in zip(tok, [[self.bos_token_id, self.eos_token_id], [1, 1]]):
            tok[k] = pad_sequence([torch.LongTensor([p[0]]+x+[p[1]]) for x in tok[k]], batch_first=True, padding_value=self.pad_token_id)
        try:
            images = torch.cat(images).float().unsqueeze(1)
        except RuntimeError:
            logging.critical('Images not working: %s' % (' '.join(list(ims))))
            return None, None
        if self.pad:
            h, w = images.shape[2:]
            images = F.pad(images, (0, self.max_dimensions[0]-w, 0, self.max_dimensions[1]-h), value=1)
        return tok, images

    def _get_size(self):
        self.size = 0
        for k in self.data:
            div, mod = divmod(len(self.data[k]), self.batchsize)
            self.size += div  # + (1 if mod > 0 else 0)

    def load(self, filename, args=[]):
        """returns a pickled version of a dataset

        Args:
            filename (str): Path to dataset
        """
        with open(filename, 'rb') as file:
            x = pickle.load(file)
        return x

    def save(self, filename):
        """save a pickled version of a dataset

        Args:
            filename (str): Path to dataset
        """
        with open(filename, 'wb') as file:
            pickle.dump(self, file)

    def update(self, **kwargs):
        for k in ['batchsize', 'shuffle', 'pad', 'keep_smaller_batches', 'test']:
            if k in kwargs:
                setattr(self, k, kwargs[k])
        if 'max_dimensions' in kwargs:
            self.max_dimensions = kwargs['max_dimensions']
            temp = {}
            for k in self.data:
                if 0 < k[0] <= self.max_dimensions[0] and 0 < k[1] <= self.max_dimensions[1]:
                    temp[k] = self.data[k]
            self.data = temp
        self._get_size()
        iter(self)


def generate_tokenizer(equations, output, vocab_size):
    from tokenizers import Tokenizer, pre_tokenizers
    from tokenizers.models import BPE
    from tokenizers.trainers import BpeTrainer
    tokenizer = Tokenizer(BPE())
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    trainer = BpeTrainer(special_tokens=["[PAD]", "[BOS]", "[EOS]"], vocab_size=vocab_size, show_progress=True)
    tokenizer.train(trainer, [equations])
    tokenizer.save(path=output, pretty=False)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Train model', add_help=False)
    parser.add_argument('-i', '--images', type=str, default=None, help='Image folder')
    parser.add_argument('-e', '--equations', type=str, default=None, help='equations text file')
    parser.add_argument('-t', '--tokenizer', default=None, help='Pretrained tokenizer file')
    parser.add_argument('-o', '--out', required=True, help='output file')
    parser.add_argument('-s', '--vocab-size', default=8000, help='vocabulary size when training a tokenizer')
    args = parser.parse_args()
    if args.images is None and args.equations is not None and args.tokenizer is None:
        print('Generate tokenizer')
        generate_tokenizer(args.equations, args.out, args.vocab_size)
    elif args.images is not None and args.equations is not None and args.tokenizer is not None:
        print('Generate dataset')
        Im2LatexDataset(args.equations, args.images, args.tokenizer).save(args.out)
    else:
        print('Not defined')
