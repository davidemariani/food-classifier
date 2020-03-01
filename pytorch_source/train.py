import argparse
import json
import os
import pandas as pd
import numpy as np

import torch
import torch.optim as optim
import torch.nn as nn
import torch.utils.data

from torchvision import datasets
import torchvision.transforms as transforms

import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau

# imports the model in model.py by name
from model import ResNetTransfer


def model_fn(model_dir):
    """Load the PyTorch model from the `model_dir` directory."""
    print("Loading model.")

    # First, load the parameters used to create the model.
    model_info = {}
    model_info_path = os.path.join(model_dir, 'model_info.pth')
    with open(model_info_path, 'rb') as f:
        model_info = torch.load(f)

    print("model_info: {}".format(model_info))

    # Determine the device and construct the model.
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Using device {}.".format(device))

    model = ResNetTransfer

    #freezing the parameters
    for param in model.parameters():
        param.requires_grad = False
    model.fc = nn.Linear(model.fc.in_features, model_info["n_classes"])

    # Load the stored model parameters.
    model_path = os.path.join(model_dir, 'model.pth')
    with open(model_path, 'rb') as f:
        model.load_state_dict(torch.load(f))

    # set to eval mode, could use no_grad
    model.to(device).eval()

    print("Done loading model.")
    return model


# Gets prepared training data for the Dataloaders
def _get_train_data_loader(img_short_side_resize, img_input_size, norm_mean, norm_std,
                           shuffle, num_workers, batch_size, datadir, trainfolder, validfolder):
    print("Get train data loader.")

    transform_train = transforms.Compose([
                    transforms.Resize(img_short_side_resize),
                    transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
                    transforms.RandomHorizontalFlip(),
                    transforms.RandomResizedCrop(img_input_size, scale=(0.08,1), ratio=(1,1)), 
                    transforms.ToTensor(),
                    transforms.Normalize(mean = norm_mean, std = norm_std)])
    transform_test = transforms.Compose([
                        transforms.Resize(img_input_size),  
                        transforms.FiveCrop(img_input_size),
                        transforms.Lambda(lambda crops: torch.stack([transforms.Compose([
                        transforms.ToTensor(),
                        transforms.Normalize(mean = norm_mean, std = norm_std)])(crop) for crop in crops]))])

    train_data = datasets.ImageFolder(datadir + trainfolder, transform_train)
    valid_data = datasets.ImageFolder(datadir + validfolder, transform_test)
    #test_data = datasets.ImageFolder(testfolder, transform_test)

    # Create the data loaders
    data = {"train" : train_data, "val":valid_data, "test" : test_data}

    train_loader = torch.utils.data.DataLoader(data["train"], batch_size=batch_size, num_workers=num_workers, shuffle=shuffle, pin_memory=True)

    #### --- NOTE on num_workers if using 5crop and batch_size for testing --- ###
    # If using the 5crop test time augmentation, num_workers = 0 (an error is raised otherwise) 
    # batch_size needs to be reduced during testing due to memory requirements
    valid_loader = torch.utils.data.DataLoader(data["val"], batch_size=int(np.floor(batch_size/5)), num_workers=0, shuffle=shuffle, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(data["test"], batch_size=int(np.floor(batch_size/5)), num_workers=0, shuffle=shuffle, pin_memory=True)

    loaders_transfer = {"train" : train_loader, "val":valid_loader, "test" : test_loader}

    return loaders_transfer

import time
import datetime

def train_epoch(model,train_loader,optimizer,criterion,device):
    """
    train steps at each epoch
    """
    train_loss = 0.0
    
    model.train()
    
    for batch_idx, (data, target) in enumerate(train_loader):
        
        data, target = data.to(device), target.to(device) # move to GPU
        
        optimizer.zero_grad() # set gradients to 0
        
        output = model(data) # get output
        
        loss = criterion(output, target) # calculate loss
        train_loss += loss.item() * data.size(0)
        
        loss.backward() # calculate gradients
        
        optimizer.step() # take step
        
    train_loss = train_loss / len(train_loader.dataset)
    return model, train_loss
        
def valid_epoch(model, valid_loader, criterion, device, fivecrop):
    """
    validation prediction steps at each epoch
    """
    valid_loss = 0.0
    
    model.eval()
    
    with torch.no_grad():
        for data, target in valid_loader:
            
            data, target = data.to(device), target.to(device) # move to GPU
            
            # if we do test time augmentation with 5crop we'll have an extra dimension in our tensor
            if fivecrop == "mean":
                bs, ncrops, c, h, w = data.size()
                output = model(data.view(-1, c, h, w)) # fuse batch size and ncrops
                output = output.view(bs, ncrops, -1).mean(1)
            elif fivecrop == "max":
                bs, ncrops, c, h, w = data.size()
                output = model(data.view(-1, c, h, w)) # fuse batch size and ncrops
                output = output.view(bs, ncrops, -1).max(1)[0]
            else:
                output = model(data)
                
            ## update the average validation loss
            loss = criterion(output, target)
            valid_loss += loss.item() * data.size(0)
            
    valid_loss = valid_loss / len(valid_loader.dataset) 
    return valid_loss


def train(n_epochs, loaders, model, optimizer, criterion, device, path_model, fivecrop = None, lr_scheduler = None):
    """
    model training
    """
    
    # initialize tracker for minimum validation loss
    valid_loss_min = np.Inf 
    train_loss = []
    valid_loss = []
    
    time_start = time.time()
    best_epoch = 0
    
    for epoch in range(1, n_epochs+1):
        
        time_start_epoch = time.time()  
        
        # train current epoch
        model, train_loss_epoch = train_epoch(model,loaders["train"],optimizer,criterion,device) 
        train_loss.append(train_loss_epoch)   
        
        # validate current epoch
        valid_loss_epoch = valid_epoch(model,loaders["val"],criterion,device,fivecrop)
        
        # learning rate scheduler
        if lr_scheduler is not None:
            lr_scheduler.step(valid_loss_epoch)
        valid_loss.append(valid_loss_epoch)  
        
        if valid_loss_epoch <= valid_loss_min: # save if validation loss is the lowest so far
            torch.save(model.state_dict(), path_model)
            valid_loss_min = valid_loss_epoch 
            best_epoch = epoch
            
        # print epoch stats
        currentDT = datetime.datetime.now()
        exact_time =  str(currentDT.hour) + ":" + str(currentDT.minute) + ":" + str(currentDT.second)
        print('Epoch {} done in {:.2f} seconds at {}. \tTraining Loss: {:.3f} \tValidation Loss: {:.3f}'.format( 
            epoch,             
            time.time() - time_start_epoch,
            exact_time,
            train_loss_epoch,
            valid_loss_epoch
            ))   
        
    # print final statistics    
    print(f"{n_epochs} epochs trained in {(time.time() - time_start):.3f} seconds. ") 
    
    print("Best model obtained at epoch {} with minimum validation loss : {:.3f}".format(best_epoch, valid_loss_min))
    
    # Load best config
    model.load_state_dict(torch.load(path_model))
    
    return model


if __name__ == '__main__':
    
    # Parameters settings
    parser = argparse.ArgumentParser()

    # SageMaker parameters
    parser.add_argument('--output-data-dir', type=str, default=os.environ['SM_OUTPUT_DATA_DIR'])
    parser.add_argument('--model-dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--data-dir', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    
    #Data preparation parameters
    parser.add_argument('--img_short_side_resize', type=int, default=256, metavar='I',
                        help='Resize to (default: 256)')
    parser.add_argument('--img_input_size', type=int, default=224, metavar='I',
                        help='Image input to ResNet (default: 224)')
    parser.add_argument('--shuffle', type=bool, default=True, metavar='I',
                        help='Dataloader shuffle (default: True)')
    parser.add_argument('--num_workers', type=int, default=16, metavar='I',
                        help='number of workers in data preparation (default: 16)')
    parser.add_argument('--trainfolder', type=str, default='train_img', metavar='I',
                        help='Name of the folder containing training data (default: train_img)')
    parser.add_argument('--validfolder', type=str, default='valid_img', metavar='I',
                        help='Name of the folder containing validation data (default: valid_img)')

    # Training Parameters
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--n_epochs', type=int, default=10, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--seed', type=int, default=42, metavar='S',
                        help='random seed (default: 42)')
    parser.add_argument('--lr', type=float, default=3e-4, metavar='LR',
                        help='learning rate (default: 3e-4)')
    
    # Model Parameters
    parser.add_argument("--n_classes", type=int, default=2, metavar="O", help="output dimension of the model (int - default: 2)")
    
    
    # args holds all passed-in arguments
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Using device {}.".format(device))

    torch.manual_seed(args.seed)

    # Load the training data.
    #setting norm_mean and norm_std
    norm_mean = [0.485, 0.456, 0.406]
    norm_std = [0.229, 0.224, 0.225]

    train_loader = _get_train_data_loader(args.img_short_side_resize, 
                                          args.img_input_size,
                                          norm_mean,
                                          norm_std,
                                          args.shuffle,
                                          args.num_workers,
                                          args.batch_size, 
                                          args.data_dir,
                                          args.trainfolder,
                                          args.validfolder)

    # Load the ResNet model
    model = ResNetTransfer

    #freezing the parameters
    for param in model_transfer.parameters():
        param.requires_grad = False

    # Replacing the last layer with a fully connected layer to retrain
    model.fc = nn.Linear(model.fc.in_features, args.n_classes)

    # Initialize the weights of the new layer
    nn.init.kaiming_normal_(model.fc.weight, nonlinearity='relu')

    # Transfer to GPU 
    model = model.to(device)

    ## Optimizer and loss function for training
    criterion_transfer = nn.CrossEntropyLoss()
    optimizer_transfer = optim.Adam(model_transfer.parameters(),lr) 
    scheduler_transfer = ReduceLROnPlateau(optimizer_transfer, 'min', verbose = True, factor = 0.5, patience = 7)

    # Trains the model 
    train(args.n_epochs, 
          loaders_transfer, 
          model, 
          optimizer_transfer, 
          criterion_transfer, 
          device, 
          args.model_dir, 
          fivecrop = "mean", 
          lr_scheduler = scheduler_transfer)

    # Keep the keys of this dictionary as they are 
    model_info_path = os.path.join(args.model_dir, 'model_info.pth')
    with open(model_info_path, 'wb') as f:
        model_info = {
            'n_classes': args.n_classes,
        }
        torch.save(model_info, f)
        
	# Save the model parameters
    model_path = os.path.join(args.model_dir, 'model.pth')
    with open(model_path, 'wb') as f:
        torch.save(model.state_dict(), f)
