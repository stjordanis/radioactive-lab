# Copyright (c) 2015-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the CC-by-NC license found in the
# LICENSE file in the root directory of this source tree.
#
import numpy as np
import torch
import torch.nn as nn
from scipy.stats import combine_pvalues
from scipy.special import betainc
import time
import os
import torchvision.transforms.transforms as transforms
import torchvision

from utils import NORMALIZE_CIFAR, Timer

import logging
from logger import setup_logger
logger = logging.getLogger(__name__)

def cosine_pvalue(c, d):
    """
    Given a dimension d, returns the probability that the dot product between
    random unitary vectors is higher than c
    """
    assert type(c) in [float, np.float64, np.float32]

    a = (d - 1) / 2.
    b = 1 / 2.

    if c >= 0:
        return 0.5 * betainc(a, b, 1-c**2)
    else:
        return 1 - cosine_pvalue(-c, d=d)

# Direct copy from original codebase.
# Less complicated then it looks. Just loops through all images in the dataset
# running them through the model in batches. They are saved in an array of dimension
# (num_images, final_layer_neurons). Offset stuff just handles copying each batch into the
# right place. Targets are saved in a similar shaped array using the same process (unused here).
def extract_features(loader, model, device, ignore_first=False, numpy=False, verbose=True):
    assert not model.training
    with torch.no_grad():
        n = len(loader.dataset)
        features = None
        # targets = np.zeros((n), dtype=int)

        offset = 0
        start = time.time()
        for elements in loader:
            if ignore_first:
                elements = elements[1:]

            img = elements[0]
            ft = model(img.to(device, non_blocking=True))
            sz = img.size(0)

            if features is None:
                d = ft.size(1)
                # features = np.zeros((n, d), dtype=np.float32)
                features = torch.zeros((n, d), dtype=torch.float32)
                all_targets = [torch.zeros((n, ), dtype=int) for _ in elements[1:]]

            features[offset:offset+sz] = ft.cpu().detach()#.numpy()

            for target, targets in zip(elements[1:], all_targets):
                targets[offset:offset+sz] = target#.numpy()

            offset += sz
            if offset % (100 * sz) == 0 and verbose:
                speed = offset / (time.time() - start)
                eta = (len(loader)*sz - offset) / speed
                print(f"Speed: {speed}, ETA: {eta}")

            # if offset >= 20000 and n >= 1e6:
            #     for i in range(10):
            #         print(20 * "*")
            #     print("WARNING: Quit extractFeatures before having extracted features of all samples")
            #     for i in range(10):
            #         print(20 * "*")
            #     break

        assert offset == n
        # if offset < n:
        #     features = features[:offset]
        #     all_targets = tuple([targets[:offset] for targets in all_targets])

        if numpy:
            features = features.numpy()
            all_targets = [targets.numpy() for targets in all_targets]

        return (features,) + tuple(all_targets)

def get_data_loader(batch_size, num_workers):

    dataset_directory = "experiments/datasets"
    test_transform = transforms.Compose([transforms.ToTensor(), NORMALIZE_CIFAR])
    test_set = torchvision.datasets.CIFAR10(dataset_directory, train=False, transform=test_transform)
    test_set_loader = torch.utils.data.DataLoader(test_set, 
                                                  batch_size=batch_size, 
                                                  num_workers=num_workers, 
                                                  shuffle=False,
                                                  pin_memory=True)

    return test_set_loader

def main(batch_size=256, num_workers=1):

    # Setup paths and logger
    experiment_directory = "experiments/radioactive/"
    output_directory = os.path.join(experiment_directory, "detect_radioactivity")
    logfile_path = os.path.join(output_directory, "logfile.txt")
    carrier_path = os.path.join(experiment_directory, "carriers.pth")
    marked_network_path = "experiments/radioactive/train_marked_classifier/checkpoint.pth"
    
    os.makedirs(output_directory, exist_ok=True)
    setup_logger(logfile_path)

    # Setup Device
    use_cuda = torch.cuda.is_available()
    print(f"CUDA Available? {use_cuda}")
    device = torch.device("cuda" if use_cuda else "cpu")

    # Setup Dataloader
    test_set_loader = get_data_loader(batch_size, num_workers)

    # Load Carrier
    carrier = torch.load(carrier_path).numpy()

    # Recreate marking network and remove fully connected layer
    marking_network = torchvision.models.resnet18(pretrained=True)
    marking_network.fc = nn.Sequential()
    marking_network.to(device)
    marking_network.eval()    

    t = Timer()
    t.start()

    # Load Target Network and remove fully connected layer
    target_checkpoint = torch.load(marked_network_path)
    target_network = torchvision.models.resnet18(pretrained=False, num_classes=10)
    target_network.load_state_dict(target_checkpoint["model_state_dict"])
    target_network.fc = nn.Sequential()
    target_network.to(device)
    target_network.eval()

    # Extract features
    logger.info("Extracting image features after running through marking and target networks.")
    features_marking, _ = extract_features(test_set_loader, marking_network, device, verbose=False)
    features_target, _  = extract_features(test_set_loader, target_network, device, verbose=False)
    features_marking = features_marking.numpy()
    features_target = features_target.numpy()

    # Align spaces
    X, residuals, rank, s = np.linalg.lstsq(features_marking, features_target)
    print("Norm of residual: %.4e" % np.linalg.norm(np.dot(features_marking, X) - features_target)**2)

    W = target_checkpoint["model_state_dict"]["fc.weight"].cpu().numpy()
    W = np.dot(W, X.T)
    W /= np.linalg.norm(W, axis=1, keepdims=True)

    # Computing scores
    scores = np.sum(W * carrier, axis=1)

    print("Mean p-value is at %d times sigma" % int(scores.mean() * np.sqrt(W.shape[0] * carrier.shape[1])))
    print("Epoch of the model: %d" % target_checkpoint["epoch"])

    p_vals = [cosine_pvalue(c, d=carrier.shape[1]) for c in list(scores)]
    print(f"log10(p)={np.log10(combine_pvalues(p_vals)[1])}")

    elapsed_time = t.stop()
    print("Total took %.2f" % (elapsed_time))

if __name__ == '__main__':
    main()
