import pathlib
import random
import os
import pandas as pd 
from glob import glob
import SimpleITK as sitk
import numpy as np
import torch
import random
from glob import glob
from sklearn.model_selection import KFold
from torch.utils.data.dataset import Dataset
import torch.nn.functional as F
from torch.utils.data._utils.collate import default_collate

def custom_collate(batch):
    batch = pad_batch_to_max_shape(batch)
    return default_collate(batch)


def determinist_collate(batch):
    batch = pad_batch_to_max_shape(batch)
    return default_collate(batch)


def pad_batch_to_max_shape(batch):
    shapes = (sample['label'].shape for sample in batch)
    _, z_sizes, y_sizes, x_sizes = list(zip(*shapes))
    maxs = [int(max(z_sizes)), int(max(y_sizes)), int(max(x_sizes))]
    for i, max_ in enumerate(maxs):
        max_stride = 16
        if max_ % max_stride != 0:
            # Make it divisible by 16
            maxs[i] = ((max_ // max_stride) + 1) * max_stride
    zmax, ymax, xmax = maxs
    for elem in batch:
        exple = elem['label']
        zpad, ypad, xpad = zmax - exple.shape[1], ymax - exple.shape[2], xmax - exple.shape[3]
        assert all(pad >= 0 for pad in (zpad, ypad, xpad)), "Negative padding value error !!"
        # free data augmentation
        left_zpad, left_ypad, left_xpad = [random.randint(0, pad) for pad in (zpad, ypad, xpad)]
        right_zpad, right_ypad, right_xpad = [pad - left_pad for pad, left_pad in
                                              zip((zpad, ypad, xpad), (left_zpad, left_ypad, left_xpad))]
        pads = (left_xpad, right_xpad, left_ypad, right_ypad, left_zpad, right_zpad)
        elem['image'], elem['label'] = F.pad(elem['image'], pads), F.pad(elem['label'], pads)
    return batch


def pad_batch1_to_compatible_size(batch):
    print(batch.shape)
    shape = batch.shape
    zyx = list(shape[-3:])
    for i, dim in enumerate(zyx):
        max_stride = 16
        if dim % max_stride != 0:
            # Make it divisible by 16
            zyx[i] = ((dim // max_stride) + 1) * max_stride
    zmax, ymax, xmax = zyx
    zpad, ypad, xpad = zmax - batch.size(2), ymax - batch.size(3), xmax - batch.size(4)
    assert all(pad >= 0 for pad in (zpad, ypad, xpad)), "Negative padding value error !!"
    pads = (0, xpad, 0, ypad, 0, zpad)
    batch = F.pad(batch, pads)
    return batch, (zpad, ypad, xpad)


def irm_min_max_preprocess(image, low_perc=1, high_perc=99):
    """Main pre-processing function used for the challenge (seems to work the best).

    Remove outliers voxels first, then min-max scale.

    Warnings
    --------
    This will not do it channel wise!!
    """

    non_zeros = image > 0
    low, high = np.percentile(image[non_zeros], [low_perc, high_perc])
    image = np.clip(image, low, high)
    #image = normalize(image)
    min_ = np.min(image)
    max_ = np.max(image)
    scale = max_ - min_
    image = (image - min_) / scale
    return image

def pad_or_crop_image(image, seg=None, target_size=(128, 144, 144),fixed=False):
    c, z, y, x = image.shape
    z_slice, y_slice, x_slice = [get_crop_slice(target, dim, fixed=fixed) for target, dim in zip(target_size, (z, y, x))]
    image = image[:, z_slice, y_slice, x_slice]
    if seg is not None:
        seg = seg[:, z_slice, y_slice, x_slice]
    todos = [get_left_right_idx_should_pad(size, dim, fixed=fixed) for size, dim in zip(target_size, [z, y, x])]
    padlist = [(0, 0)]  # channel dim
    for to_pad in todos:
        if to_pad[0]:
            padlist.append((to_pad[1], to_pad[2]))
        else:
            padlist.append((0, 0))
    image = np.pad(image, padlist, mode='constant')
    if seg is not None:
        seg = np.pad(seg, padlist, mode='constant')
        return image, seg
    return image


def get_left_right_idx_should_pad(target_size, dim, fixed=False):
    if dim >= target_size:
        return [False]
    elif dim < target_size:
        pad_extent = target_size - dim
        left = random.randint(0, pad_extent) if not fixed else pad_extent//2
        right = pad_extent - left
        return True, left, right
    
def get_crop_slice(target_size, dim, fixed=False):
    if dim > target_size:
        crop_extent = dim - target_size
        left = random.randint(0, crop_extent) if not fixed else crop_extent//2
        right = crop_extent - left
        return slice(left, dim - right)
    elif dim <= target_size:
        return slice(0, dim)


    
class MNMS(Dataset):
    def __init__(self, patients_dir, ids_vendors=None, training=True, data_aug=False,
                 no_seg=False, normalisation="minmax"):
        super(MNMS, self).__init__()
        self.normalisation = normalisation
        self.data_aug = data_aug
        self.training = training
        self.datas = patients_dir
        self.ids_vendors = ids_vendors
        self.validation = no_seg


    def __getitem__(self, idx):
        _patient = self.datas[idx]
        patients_id = os.path.basename(os.path.dirname(_patient))
        patient_image = self.load_nii(_patient.replace('_gt',''))
        patient_label = self.load_nii(_patient)
        patient_image = irm_min_max_preprocess(patient_image)#{key: irm_min_max_preprocess(patient_image[key]) for key in patient_image}

        patient_image = patient_image[None]
        patient_label = patient_label[None]
        if self.training:
            # Remove maximum extent of the zero-background to make future crop more useful
            z_indexes, y_indexes, x_indexes = np.nonzero(np.sum(patient_image, axis=0) != 0)
            # Add 1 pixel in each side
            zmin, ymin, xmin = [max(0, int(np.min(arr) - 1)) for arr in (z_indexes, y_indexes, x_indexes)]
            zmax, ymax, xmax = [int(np.max(arr) + 1) for arr in (z_indexes, y_indexes, x_indexes)]
            patient_image = patient_image[:, zmin:zmax, ymin:ymax, xmin:xmax]
            patient_label = patient_label[:, zmin:zmax, ymin:ymax, xmin:xmax]
            patient_image, patient_label = pad_or_crop_image(patient_image, patient_label, target_size=(24, 192, 192))
        else:
            z_indexes, y_indexes, x_indexes = np.nonzero(np.sum(patient_image, axis=0) != 0)
            # Add 1 pixel in each side
            zmin, ymin, xmin = [max(0, int(np.min(arr) - 1)) for arr in (z_indexes, y_indexes, x_indexes)]
            zmax, ymax, xmax = [int(np.max(arr) + 1) for arr in (z_indexes, y_indexes, x_indexes)]
            patient_image = patient_image[:, zmin:zmax, ymin:ymax, xmin:xmax]
            patient_label = patient_label[:, zmin:zmax, ymin:ymax, xmin:xmax]
            patient_image, patient_label = pad_or_crop_image(patient_image, patient_label, target_size=(24, 192, 192), fixed=True)

        patient_image, patient_label = patient_image.astype("float16"), patient_label.astype("uint8")
        patient_image, patient_label = [torch.from_numpy(x) for x in [patient_image, patient_label]]
        return dict(patient_id=patients_id, vendor=self.ids_vendors[patients_id],
                    image=patient_image, label=patient_label,
                    crop_indexes=((zmin, zmax), (ymin, ymax), (xmin, xmax)),
                    )

    @staticmethod
    def load_nii(path_folder):
        return sitk.GetArrayFromImage(sitk.ReadImage(str(path_folder)))

    def __len__(self):
        return len(self.datas)
    
class Brats(Dataset):
    def __init__(self, patients_dir, benchmarking=False, training=True, data_aug=False,
                 no_seg=False, normalisation="minmax"):
        super(Brats, self).__init__()
        self.benchmarking = benchmarking
        self.normalisation = normalisation
        self.data_aug = data_aug
        self.training = training
        self.datas = []
        self.validation = no_seg
        self.patterns = ["_t1", "_t1ce", "_t2", "_flair"]
        if not no_seg:
            self.patterns += ["_seg"]
        for patient_dir in patients_dir:
            patient_id = patient_dir.name
            paths = [patient_dir / f"{patient_id}{value}.nii.gz" for value in self.patterns]
            patient = dict(
                id=patient_id, t1=paths[0], t1ce=paths[1],
                t2=paths[2], flair=paths[3], seg=paths[4] if not no_seg else None
            )
            self.datas.append(patient)

    def __getitem__(self, idx):
        _patient = self.datas[idx]
        patient_image = {key: self.load_nii(_patient[key]) for key in _patient if key not in ["id", "seg"]}
        if _patient["seg"] is not None:
            patient_label = self.load_nii(_patient["seg"])
        if self.normalisation == "minmax":
            patient_image = {key: irm_min_max_preprocess(patient_image[key]) for key in patient_image}
        elif self.normalisation == "zscore":
            patient_image = {key: zscore_normalise(patient_image[key]) for key in patient_image}
        patient_image = np.stack([patient_image[key] for key in patient_image])
        patient_label[patient_label==4] = 3
        patient_label = patient_label[None,:,:,:]
        if self.training:
            # Remove maximum extent of the zero-background to make future crop more useful
            z_indexes, y_indexes, x_indexes = np.nonzero(np.sum(patient_image, axis=0) != 0)
            # Add 1 pixel in each side
            zmin, ymin, xmin = [max(0, int(np.min(arr) - 1)) for arr in (z_indexes, y_indexes, x_indexes)]
            zmax, ymax, xmax = [int(np.max(arr) + 1) for arr in (z_indexes, y_indexes, x_indexes)]
            patient_image = patient_image[:, zmin:zmax, ymin:ymax, xmin:xmax]
            patient_label = patient_label[:, zmin:zmax, ymin:ymax, xmin:xmax]
            # default to 128, 128, 128
            patient_image, patient_label = pad_or_crop_image(patient_image, patient_label, target_size=(128, 128, 128))
        else:
            z_indexes, y_indexes, x_indexes = np.nonzero(np.sum(patient_image, axis=0) != 0)
            # Add 1 pixel in each side
            zmin, ymin, xmin = [max(0, int(np.min(arr) - 1)) for arr in (z_indexes, y_indexes, x_indexes)]
            zmax, ymax, xmax = [int(np.max(arr) + 1) for arr in (z_indexes, y_indexes, x_indexes)]
            patient_image = patient_image[:, zmin:zmax, ymin:ymax, xmin:xmax]
            patient_label = patient_label[:, zmin:zmax, ymin:ymax, xmin:xmax]
        
        patient_image, patient_label = patient_image.astype("float16"), patient_label.astype("long")
        patient_image, patient_label = [torch.from_numpy(x) for x in [patient_image, patient_label]]
        # onehot
        #patient_label_oh = (patient_label.squeeze()[...,None] == torch.arange(4).reshape(1, 4))
        #patient_label_oh = patient_label_oh.permute(3,0,1,2).float()
        return dict(patient_id=_patient["id"],
                    image=patient_image, label=patient_label,
                    seg_path=str(_patient["seg"]), #if not self.validation else str(_patient["t1"]),
                    crop_indexes=((zmin, zmax), (ymin, ymax), (xmin, xmax)),
                    #et_present=0,
                    #supervised=True,
                    )
    
    @staticmethod
    def load_nii(path_folder):
        return sitk.GetArrayFromImage(sitk.ReadImage(str(path_folder)))

    def __len__(self):
        return len(self.datas)

    
def get_datasets19(seed, data_root='/media/mobarak/data/Datasets/MNMS', no_seg=False, on="train", full=False,
                 fold_number=0, normalisation="minmax"):

    data_root = data_root
    data_info_fir = os.path.join(data_root,'201014_M&Ms_Dataset_Information.csv')
    data_info = pd.read_csv(data_info_fir) 
    patient_ids = data_info['External code']
    patient_ids_vendors = dict( [(patients_ids, data_info['Vendor'][i]) for i, patients_ids in enumerate(data_info['External code'])] )
    #patients_dir_train = glob(os.path.join(data_root,'Training/Labeled_Resampled/**/*_gt.nii.gz'))
    patients_dir_valid = glob(os.path.join(data_root,'Labeled_Resampled/**/*_gt.nii.gz'))
    #patients_dir_train.sort()
    patients_dir_valid.sort()
    #train_dataset = MNMS(patients_dir_train, training=True, normalisation=normalisation, ids_vendors=patient_ids_vendors)
    val_dataset = MNMS(patients_dir_valid, training=False, data_aug=False, normalisation=normalisation, ids_vendors=patient_ids_vendors)
    #bench_dataset = MNMS(patients_dir_valid, training=False, normalisation=normalisation, ids_vendors=patient_ids_vendors)
    return  val_dataset#, bench_dataset

def get_datasets_BraTS19(seed, data_root=None, no_seg=False, on="train", full=False,
                 fold_number=0, normalisation="minmax"):

    data_root = pathlib.Path(data_root)
    base_folder_valid = pathlib.Path('cv_valid/').resolve()
    patients_dir_valid = sorted([data_root/x.name for x in base_folder_valid.iterdir() if (data_root/x.name).is_dir()])
    val_dataset = Brats(patients_dir_valid, training=False, data_aug=False, normalisation=normalisation)
    return val_dataset



