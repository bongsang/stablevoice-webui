import os
import sys
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

now_dir = os.getcwd()
sys.path.append(now_dir)
load_dotenv()
import json
import logging
import pathlib
import platform
import shutil
import threading
import traceback
import warnings
from random import shuffle
from subprocess import Popen
from time import sleep

import fairseq
import faiss
import gradio as gr
import numpy as np
import torch
from sklearn.cluster import MiniBatchKMeans

from configs.config import Config
from infer.lib.train.process_ckpt import (
    change_info,
    extract_small_model,
    merge,
    show_info,
)
from infer.modules.uvr5.modules import uvr
from infer.modules.vc.modules import VC

logging.getLogger("numba").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

tmp = os.path.join(now_dir, "TEMP")
shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree("%s/runtime/Lib/site-packages/infer_pack" % (now_dir), ignore_errors=True)
shutil.rmtree("%s/runtime/Lib/site-packages/uvr5_pack" % (now_dir), ignore_errors=True)
os.makedirs(tmp, exist_ok=True)
os.makedirs(os.path.join(now_dir, "logs"), exist_ok=True)
os.makedirs(os.path.join(now_dir, "assets/weights"), exist_ok=True)
os.environ["TEMP"] = tmp
warnings.filterwarnings("ignore")
torch.manual_seed(114514)


config = Config()
vc = VC(config)


if config.dml == True:

    def forward_dml(ctx, x, scale):
        ctx.scale = scale
        res = x.clone().detach()
        return res

    fairseq.modules.grad_multiply.GradMultiply.forward = forward_dml

# Check how many GPUs are available
ngpu = torch.cuda.device_count()
gpu_infos = []
mem = []
if_gpu_ok = False

if torch.cuda.is_available() or ngpu != 0:
    for i in range(ngpu):
        gpu_name = torch.cuda.get_device_name(i)
        if any(
            value in gpu_name.upper()
            for value in [
                "10",
                "16",
                "20",
                "30",
                "40",
                "A2",
                "A3",
                "A4",
                "P4",
                "A50",
                "500",
                "A60",
                "70",
                "80",
                "90",
                "M4",
                "T4",
                "TITAN",
                "4060",
                "L",
                "6000",
            ]
        ):
            # A10#A100#V100#A40#P40#M40#K80#A4500
            if_gpu_ok = True  # At least one usable Nvidia card
            gpu_infos.append("%s\t%s" % (i, gpu_name))
            mem.append(
                int(
                    torch.cuda.get_device_properties(i).total_memory
                    / 1024
                    / 1024
                    / 1024
                    + 0.4
                )
            )
if if_gpu_ok and len(gpu_infos) > 0:
    gpu_info = "\n".join(gpu_infos)
    default_batch_size = min(mem) // 2
else:
    gpu_info = (
        "Unfortunately, there is no compatible GPU available to support your training."
    )
    default_batch_size = 1
gpus = "-".join([i[0] for i in gpu_infos])

weight_root = os.getenv("weight_root")
weight_uvr5_root = os.getenv("weight_uvr5_root")
index_root = os.getenv("index_root")
outside_index_root = os.getenv("outside_index_root")

names = []
for name in os.listdir(weight_root):
    if name.endswith(".pth"):
        names.append(name)
index_paths = []


class ToolButton(gr.Button, gr.components.FormComponent):
    """Small button with single emoji as text, fits inside gradio forms"""

    def __init__(self, **kwargs):
        super().__init__(variant="tool", **kwargs)

    def get_block_name(self):
        return "button"
    

def lookup_indices(index_root):
    global index_paths
    for root, dirs, files in os.walk(index_root, topdown=False):
        for name in files:
            if name.endswith(".index") and "trained" not in name:
                index_paths.append("%s/%s" % (root, name))


lookup_indices(index_root)
lookup_indices(outside_index_root)
uvr5_names = []
for name in os.listdir(weight_uvr5_root):
    if name.endswith(".pth") or "onnx" in name:
        uvr5_names.append(name.replace(".pth", ""))


def change_choices():
    names = []
    for name in os.listdir(weight_root):
        if name.endswith(".pth"):
            names.append(name)
    index_paths = []
    for root, dirs, files in os.walk(index_root, topdown=False):
        for name in files:
            if name.endswith(".index") and "trained" not in name:
                index_paths.append("%s/%s" % (root, name))
    return {"choices": sorted(names), "__type__": "update"}, {
        "choices": sorted(index_paths),
        "__type__": "update",
    }


def clean():
    return {"value": "", "__type__": "update"}


def export_onnx(ModelPath, ExportedPath):
    from infer.modules.onnx.export import export_onnx as eo

    eo(ModelPath, ExportedPath)


sr_dict = {
    "32k": 32000,
    "40k": 40000,
    "48k": 48000,
}


def if_done(done, p):
    while 1:
        if p.poll() is None:
            sleep(0.5)
        else:
            break
    done[0] = True


def if_done_multi(done, ps):
    while 1:
        flag = 1
        for p in ps:
            if p.poll() is None:
                flag = 0
                sleep(0.5)
                break
        if flag == 1:
            break
    done[0] = True


def preprocess_dataset(trainset_dir, exp_dir, sr, n_p):
    sr = sr_dict[sr]
    os.makedirs("%s/logs/%s" % (now_dir, exp_dir), exist_ok=True)
    f = open("%s/logs/%s/preprocess.log" % (now_dir, exp_dir), "w")
    f.close()
    cmd = '"%s" infer/modules/train/preprocess.py "%s" %s %s "%s/logs/%s" %s %.1f' % (
        config.python_cmd,
        trainset_dir,
        sr,
        n_p,
        now_dir,
        exp_dir,
        config.noparallel,
        config.preprocess_per,
    )
    logger.info("Execute: " + cmd)
    # , stdin=PIPE, stdout=PIPE,stderr=PIPE,cwd=now_dir
    p = Popen(cmd, shell=True)
    done = [False]
    threading.Thread(
        target=if_done,
        args=(
            done,
            p,
        ),
    ).start()
    while 1:
        with open("%s/logs/%s/preprocess.log" % (now_dir, exp_dir), "r") as f:
            yield (f.read())
        sleep(1)
        if done[0]:
            break
    with open("%s/logs/%s/preprocess.log" % (now_dir, exp_dir), "r") as f:
        log = f.read()
    logger.info(log)
    yield log


def extract_f0_feature(gpus, n_p, f0method, if_f0, exp_dir, version19, gpus_rmvpe):
    gpus = gpus.split("-")
    os.makedirs("%s/logs/%s" % (now_dir, exp_dir), exist_ok=True)
    f = open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "w")
    f.close()
    if if_f0:
        if f0method != "rmvpe_gpu":
            cmd = (
                '"%s" infer/modules/train/extract/extract_f0_print.py "%s/logs/%s" %s %s'
                % (
                    config.python_cmd,
                    now_dir,
                    exp_dir,
                    n_p,
                    f0method,
                )
            )
            logger.info("Execute: " + cmd)
            p = Popen(
                cmd, shell=True, cwd=now_dir
            )  # , stdin=PIPE, stdout=PIPE,stderr=PIPE
            done = [False]
            threading.Thread(
                target=if_done,
                args=(
                    done,
                    p,
                ),
            ).start()
        else:
            if gpus_rmvpe != "-":
                gpus_rmvpe = gpus_rmvpe.split("-")
                leng = len(gpus_rmvpe)
                ps = []
                for idx, n_g in enumerate(gpus_rmvpe):
                    cmd = (
                        '"%s" infer/modules/train/extract/extract_f0_rmvpe.py %s %s %s "%s/logs/%s" %s '
                        % (
                            config.python_cmd,
                            leng,
                            idx,
                            n_g,
                            now_dir,
                            exp_dir,
                            config.is_half,
                        )
                    )
                    logger.info("Execute: " + cmd)
                    p = Popen(
                        cmd, shell=True, cwd=now_dir
                    )  # , shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=now_dir
                    ps.append(p)
                done = [False]
                threading.Thread(
                    target=if_done_multi,  #
                    args=(
                        done,
                        ps,
                    ),
                ).start()
            else:
                cmd = (
                    config.python_cmd
                    + ' infer/modules/train/extract/extract_f0_rmvpe_dml.py "%s/logs/%s" '
                    % (
                        now_dir,
                        exp_dir,
                    )
                )
                logger.info("Execute: " + cmd)
                p = Popen(
                    cmd, shell=True, cwd=now_dir
                )  # , shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=now_dir
                p.wait()
                done = [True]
        while 1:
            with open(
                "%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r"
            ) as f:
                yield (f.read())
            sleep(1)
            if done[0]:
                break
        with open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r") as f:
            log = f.read()
        logger.info(log)
        yield log
    """
    n_part=int(sys.argv[1])
    i_part=int(sys.argv[2])
    i_gpu=sys.argv[3]
    exp_dir=sys.argv[4]
    os.environ["CUDA_VISIBLE_DEVICES"]=str(i_gpu)
    """
    leng = len(gpus)
    ps = []
    for idx, n_g in enumerate(gpus):
        cmd = (
            '"%s" infer/modules/train/extract_feature_print.py %s %s %s %s "%s/logs/%s" %s %s'
            % (
                config.python_cmd,
                config.device,
                leng,
                idx,
                n_g,
                now_dir,
                exp_dir,
                version19,
                config.is_half,
            )
        )
        logger.info("Execute: " + cmd)
        p = Popen(
            cmd, shell=True, cwd=now_dir
        )  # , shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=now_dir
        ps.append(p)
    done = [False]
    threading.Thread(
        target=if_done_multi,
        args=(
            done,
            ps,
        ),
    ).start()
    while 1:
        with open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r") as f:
            yield (f.read())
        sleep(1)
        if done[0]:
            break
    with open("%s/logs/%s/extract_f0_feature.log" % (now_dir, exp_dir), "r") as f:
        log = f.read()
    logger.info(log)
    yield log


def get_pretrained_models(path_str, f0_str, sr2):
    if_pretrained_generator_exist = os.access(
        "assets/pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2), os.F_OK
    )
    if_pretrained_discriminator_exist = os.access(
        "assets/pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2), os.F_OK
    )
    if not if_pretrained_generator_exist:
        logger.warning(
            "assets/pretrained%s/%sG%s.pth not exist, will not use pretrained model",
            path_str,
            f0_str,
            sr2,
        )
    if not if_pretrained_discriminator_exist:
        logger.warning(
            "assets/pretrained%s/%sD%s.pth not exist, will not use pretrained model",
            path_str,
            f0_str,
            sr2,
        )
    return (
        (
            "assets/pretrained%s/%sG%s.pth" % (path_str, f0_str, sr2)
            if if_pretrained_generator_exist
            else ""
        ),
        (
            "assets/pretrained%s/%sD%s.pth" % (path_str, f0_str, sr2)
            if if_pretrained_discriminator_exist
            else ""
        ),
    )


def change_sr2(sr2, if_f0_3, version19):
    path_str = "" if version19 == "v1" else "_v2"
    f0_str = "f0" if if_f0_3 else ""
    return get_pretrained_models(path_str, f0_str, sr2)


def change_version19(sr2, if_f0_3, version19):
    path_str = "" if version19 == "v1" else "_v2"
    if sr2 == "32k" and version19 == "v1":
        sr2 = "40k"
    to_return_sr2 = (
        {"choices": ["40k", "48k"], "__type__": "update", "value": sr2}
        if version19 == "v1"
        else {"choices": ["40k", "48k", "32k"], "__type__": "update", "value": sr2}
    )
    f0_str = "f0" if if_f0_3 else ""
    return (
        *get_pretrained_models(path_str, f0_str, sr2),
        to_return_sr2,
    )


def change_f0(if_f0_3, sr2, version19):  # f0method8,pretrained_G14,pretrained_D15
    path_str = "" if version19 == "v1" else "_v2"
    return (
        {"visible": if_f0_3, "__type__": "update"},
        {"visible": if_f0_3, "__type__": "update"},
        *get_pretrained_models(path_str, "f0" if if_f0_3 == True else "", sr2),
    )


# but3.click(click_train,[exp_dir1,sr2,if_f0_3,save_epoch10,total_epoch11,batch_size12,if_save_latest13,pretrained_G14,pretrained_D15,gpus16])
def click_train(
    exp_dir1,
    sr2,
    if_f0_3,
    spk_id5,
    save_epoch10,
    total_epoch11,
    batch_size12,
    if_save_latest13,
    pretrained_G14,
    pretrained_D15,
    gpus16,
    if_cache_gpu17,
    if_save_every_weights18,
    version19,
):
    exp_dir = "%s/logs/%s" % (now_dir, exp_dir1)
    os.makedirs(exp_dir, exist_ok=True)
    gt_wavs_dir = "%s/0_gt_wavs" % (exp_dir)
    feature_dir = (
        "%s/3_feature256" % (exp_dir)
        if version19 == "v1"
        else "%s/3_feature768" % (exp_dir)
    )
    if if_f0_3:
        f0_dir = "%s/2a_f0" % (exp_dir)
        f0nsf_dir = "%s/2b-f0nsf" % (exp_dir)
        names = (
            set([name.split(".")[0] for name in os.listdir(gt_wavs_dir)])
            & set([name.split(".")[0] for name in os.listdir(feature_dir)])
            & set([name.split(".")[0] for name in os.listdir(f0_dir)])
            & set([name.split(".")[0] for name in os.listdir(f0nsf_dir)])
        )
    else:
        names = set([name.split(".")[0] for name in os.listdir(gt_wavs_dir)]) & set(
            [name.split(".")[0] for name in os.listdir(feature_dir)]
        )
    opt = []
    for name in names:
        if if_f0_3:
            opt.append(
                "%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|%s"
                % (
                    gt_wavs_dir.replace("\\", "\\\\"),
                    name,
                    feature_dir.replace("\\", "\\\\"),
                    name,
                    f0_dir.replace("\\", "\\\\"),
                    name,
                    f0nsf_dir.replace("\\", "\\\\"),
                    name,
                    spk_id5,
                )
            )
        else:
            opt.append(
                "%s/%s.wav|%s/%s.npy|%s"
                % (
                    gt_wavs_dir.replace("\\", "\\\\"),
                    name,
                    feature_dir.replace("\\", "\\\\"),
                    name,
                    spk_id5,
                )
            )
    fea_dim = 256 if version19 == "v1" else 768
    if if_f0_3:
        for _ in range(2):
            opt.append(
                "%s/logs/mute/0_gt_wavs/mute%s.wav|%s/logs/mute/3_feature%s/mute.npy|%s/logs/mute/2a_f0/mute.wav.npy|%s/logs/mute/2b-f0nsf/mute.wav.npy|%s"
                % (now_dir, sr2, now_dir, fea_dim, now_dir, now_dir, spk_id5)
            )
    else:
        for _ in range(2):
            opt.append(
                "%s/logs/mute/0_gt_wavs/mute%s.wav|%s/logs/mute/3_feature%s/mute.npy|%s"
                % (now_dir, sr2, now_dir, fea_dim, spk_id5)
            )
    shuffle(opt)
    with open("%s/filelist.txt" % exp_dir, "w") as f:
        f.write("\n".join(opt))
    logger.debug("Write filelist done")
    # cmd = python_cmd + " train_nsf_sim_cache_sid_load_pretrain.py -e mi-test -sr 40k -f0 1 -bs 4 -g 0 -te 10 -se 5 -pg pretrained/f0G40k.pth -pd pretrained/f0D40k.pth -l 1 -c 0"
    logger.info("Use gpus: %s", str(gpus16))
    if pretrained_G14 == "":
        logger.info("No pretrained Generator")
    if pretrained_D15 == "":
        logger.info("No pretrained Discriminator")
    if version19 == "v1" or sr2 == "40k":
        config_path = "v1/%s.json" % sr2
    else:
        config_path = "v2/%s.json" % sr2
    config_save_path = os.path.join(exp_dir, "config.json")
    if not pathlib.Path(config_save_path).exists():
        with open(config_save_path, "w", encoding="utf-8") as f:
            json.dump(
                config.json_config[config_path],
                f,
                ensure_ascii=False,
                indent=4,
                sort_keys=True,
            )
            f.write("\n")
    if gpus16:
        cmd = (
            '"%s" infer/modules/train/train.py -e "%s" -sr %s -f0 %s -bs %s -g %s -te %s -se %s %s %s -l %s -c %s -sw %s -v %s'
            % (
                config.python_cmd,
                exp_dir1,
                sr2,
                1 if if_f0_3 else 0,
                batch_size12,
                gpus16,
                total_epoch11,
                save_epoch10,
                "-pg %s" % pretrained_G14 if pretrained_G14 != "" else "",
                "-pd %s" % pretrained_D15 if pretrained_D15 != "" else "",
                1 if if_save_latest13 == "Yes" else 0,
                1 if if_cache_gpu17 == "Yes" else 0,
                1 if if_save_every_weights18 == "Yes" else 0,
                version19,
            )
        )
    else:
        cmd = (
            '"%s" infer/modules/train/train.py -e "%s" -sr %s -f0 %s -bs %s -te %s -se %s %s %s -l %s -c %s -sw %s -v %s'
            % (
                config.python_cmd,
                exp_dir1,
                sr2,
                1 if if_f0_3 else 0,
                batch_size12,
                total_epoch11,
                save_epoch10,
                "-pg %s" % pretrained_G14 if pretrained_G14 != "" else "",
                "-pd %s" % pretrained_D15 if pretrained_D15 != "" else "",
                1 if if_save_latest13 == "Yes" else 0,
                1 if if_cache_gpu17 == "Yes" else 0,
                1 if if_save_every_weights18 == "Yes" else 0,
                version19,
            )
        )
    logger.info("Execute: " + cmd)
    p = Popen(cmd, shell=True, cwd=now_dir)
    p.wait()
    return "After the training is finished, you can view the console training log or train.log in the experiment folder."


# but4.click(train_index, [exp_dir1], info3)
def train_index(exp_dir1, version19):
    # exp_dir = "%s/logs/%s" % (now_dir, exp_dir1)
    exp_dir = "logs/%s" % (exp_dir1)
    os.makedirs(exp_dir, exist_ok=True)
    feature_dir = (
        "%s/3_feature256" % (exp_dir)
        if version19 == "v1"
        else "%s/3_feature768" % (exp_dir)
    )
    if not os.path.exists(feature_dir):
        return "Please perform feature extraction first!"
    listdir_res = list(os.listdir(feature_dir))
    if len(listdir_res) == 0:
        return "Please perform feature extraction first!"
    infos = []
    npys = []
    for name in sorted(listdir_res):
        phone = np.load("%s/%s" % (feature_dir, name))
        npys.append(phone)
    big_npy = np.concatenate(npys, 0)
    big_npy_idx = np.arange(big_npy.shape[0])
    np.random.shuffle(big_npy_idx)
    big_npy = big_npy[big_npy_idx]
    if big_npy.shape[0] > 2e5:
        infos.append("Trying doing kmeans %s shape to 10k centers." % big_npy.shape[0])
        yield "\n".join(infos)
        try:
            big_npy = (
                MiniBatchKMeans(
                    n_clusters=10000,
                    verbose=True,
                    batch_size=256 * config.n_cpu,
                    compute_labels=False,
                    init="random",
                )
                .fit(big_npy)
                .cluster_centers_
            )
        except:
            info = traceback.format_exc()
            logger.info(info)
            infos.append(info)
            yield "\n".join(infos)

    np.save("%s/total_fea.npy" % exp_dir, big_npy)
    n_ivf = min(int(16 * np.sqrt(big_npy.shape[0])), big_npy.shape[0] // 39)
    infos.append("%s,%s" % (big_npy.shape, n_ivf))
    yield "\n".join(infos)
    index = faiss.index_factory(256 if version19 == "v1" else 768, "IVF%s,Flat" % n_ivf)
    # index = faiss.index_factory(256if version19=="v1"else 768, "IVF%s,PQ128x4fs,RFlat"%n_ivf)
    infos.append("training")
    yield "\n".join(infos)
    index_ivf = faiss.extract_index_ivf(index)  #
    index_ivf.nprobe = 1
    index.train(big_npy)
    faiss.write_index(
        index,
        "%s/trained_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (exp_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
    )
    infos.append("adding")
    yield "\n".join(infos)
    batch_size_add = 8192
    for i in range(0, big_npy.shape[0], batch_size_add):
        index.add(big_npy[i : i + batch_size_add])
    faiss.write_index(
        index,
        "%s/added_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (exp_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
    )
    infos.append(
        "Successfully built index! added_IVF%s_Flat_nprobe_%s_%s_%s.index"
        % (n_ivf, index_ivf.nprobe, exp_dir1, version19)
    )
    try:
        link = os.link if platform.system() == "Windows" else os.symlink
        link(
            "%s/added_IVF%s_Flat_nprobe_%s_%s_%s.index"
            % (exp_dir, n_ivf, index_ivf.nprobe, exp_dir1, version19),
            "%s/%s_IVF%s_Flat_nprobe_%s_%s_%s.index"
            % (
                outside_index_root,
                exp_dir1,
                n_ivf,
                index_ivf.nprobe,
                exp_dir1,
                version19,
            ),
        )
        infos.append("Link index to external-%s" % (outside_index_root))
    except:
        infos.append("Link index to external-%s失败" % (outside_index_root))

    # faiss.write_index(index, '%s/added_IVF%s_Flat_FastScan_%s.index'%(exp_dir,n_ivf,version19))
    # infos.append("Successfully built index, added_IVF%s_Flat_FastScan_%s.index"%(n_ivf,version19))
    yield "\n".join(infos)


# but5.click(train1key, [exp_dir1, sr2, if_f0_3, trainset_dir4, spk_id5, gpus6, np7, f0method8, save_epoch10, total_epoch11, batch_size12, if_save_latest13, pretrained_G14, pretrained_D15, gpus16, if_cache_gpu17], info3)
def train1key(
    exp_dir1,
    sr2,
    if_f0_3,
    trainset_dir4,
    spk_id5,
    np7,
    f0method8,
    save_epoch10,
    total_epoch11,
    batch_size12,
    if_save_latest13,
    pretrained_G14,
    pretrained_D15,
    gpus16,
    if_cache_gpu17,
    if_save_every_weights18,
    version19,
    gpus_rmvpe,
):
    infos = []

    def get_info_str(strr):
        infos.append(strr)
        return "\n".join(infos)

    yield "Step 1: Processing data"
    [get_info_str(_) for _ in preprocess_dataset(trainset_dir4, exp_dir1, sr2, np7)]

    yield "step2:Pitch extraction & feature extraction"
    [
        get_info_str(_)
        for _ in extract_f0_feature(
            gpus16, np7, f0method8, if_f0_3, exp_dir1, version19, gpus_rmvpe
        )
    ]

    yield "Step 3a: Model training started"
    click_train(
        exp_dir1,
        sr2,
        if_f0_3,
        spk_id5,
        save_epoch10,
        total_epoch11,
        batch_size12,
        if_save_latest13,
        pretrained_G14,
        pretrained_D15,
        gpus16,
        if_cache_gpu17,
        if_save_every_weights18,
        version19,
    )
    yield "Training complete. You can check the training logs in the console or the 'train.log' file under the experiment folder."

    [get_info_str(_) for _ in train_index(exp_dir1, version19)]
    yield "All processes have been completed!"


# ckpt_path2.change(change_info_,[ckpt_path2],[sr__,if_f0__])
def change_info_(ckpt_path):
    if not os.path.exists(ckpt_path.replace(os.path.basename(ckpt_path), "train.log")):
        return {"__type__": "update"}, {"__type__": "update"}, {"__type__": "update"}
    try:
        with open(
            ckpt_path.replace(os.path.basename(ckpt_path), "train.log"), "r"
        ) as f:
            info = eval(f.read().strip("\n").split("\n")[0].split("\t")[-1])
            sr, f0 = info["sample_rate"], info["if_f0"]
            version = "v2" if ("version" in info and info["version"] == "v2") else "v1"
            return sr, str(f0), version
    except:
        traceback.print_exc()
        return {"__type__": "update"}, {"__type__": "update"}, {"__type__": "update"}


F0GPUVisible = config.dml == False


def change_f0_method(f0method8):
    if f0method8 == "rmvpe_gpu":
        visible = F0GPUVisible
    else:
        visible = False
    return {"visible": visible, "__type__": "update"}


# Assets Manager
assets_dir = "assets/"
assets_file_path = os.path.join(assets_dir, "assets.txt")
with open(assets_file_path, "r") as f:
    assets_list = f.read().splitlines()


def download_file(asset_name):
    if asset_name.split(".")[-1] == "exe":
        download_asset_name = "bin/" + asset_name
    elif "hubert" in asset_name:
        download_asset_name = "hubert/" + asset_name
    elif "rmvpe" in asset_name:
        download_asset_name = "rmvpe/" + asset_name
    else:
        download_asset_name = asset_name
    asset_path = os.path.join(assets_dir, download_asset_name)
    base_url = "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/"
    url = f"{base_url}{asset_name}?download=true"
    os.makedirs(os.path.dirname(asset_path), exist_ok=True)
    response = requests.get(url)
    if response.status_code == 200:
        with open(asset_path, "wb") as file:
            file.write(response.content)
        return f"Downloaded: {asset_name}"
    else:
        return f"Failed to download: {asset_name}"


def remove_file(asset_name):
    if asset_name.split(".")[-1] == "exe":
        asset_name = "bin/" + asset_name
    elif "hubert" in asset_name:
        asset_name = "hubert/" + asset_name
    elif "rmvpe" in asset_name:
        asset_name = "rmvpe/" + asset_name
    asset_path = os.path.join(assets_dir, asset_name)

    if os.path.exists(asset_path):
        os.remove(asset_path)
        return f"Removed: {asset_name}"
    else:
        return f"File not found: {asset_name}"


def get_preselected_files():
    downloaded_files = []
    for asset_name in assets_list:
        if asset_name.split(".")[-1] == "exe":
            download_asset_name = "bin/" + asset_name
        elif "hubert" in asset_name:
            download_asset_name = "hubert/" + asset_name
        elif "rmvpe" in asset_name:
            download_asset_name = "rmvpe/" + asset_name
        else:
            download_asset_name = asset_name

        asset_path = os.path.join(assets_dir, download_asset_name)

        if os.path.exists(asset_path):
            downloaded_files.append(asset_name)

    return downloaded_files


def update_buttons(asset_name):
    if asset_name.split(".")[-1] == "exe":
        download_asset_name = "bin/" + asset_name
    elif "hubert" in asset_name:
        download_asset_name = "hubert/" + asset_name
    elif "rmvpe" in asset_name:
        download_asset_name = "rmvpe/" + asset_name
    else:
        download_asset_name = asset_name

    asset_path = os.path.join(assets_dir, download_asset_name)
    return os.path.exists(asset_path), not os.path.exists(asset_path)


with gr.Blocks(title="Stable Voice", theme=gr.themes.Soft()) as app:
    gr.Markdown("# Stable Voice WebUI")
    gr.Markdown(
        "### Welcome to the Stable Voice! This is a web interface for the Stable Voice project. You can use this interface to train and use voice conversion models."
    )

    with gr.Tabs():
        with gr.Tab("Model Manager"):
            output_box = gr.Textbox(label="Output", interactive=False)
            checkboxes = []
            download_buttons = []
            remove_buttons = []
            for asset_name in assets_list:
                with gr.Row():
                    with gr.Column(scale=2):
                        checkbox = gr.Checkbox(
                            label=asset_name,
                            value=asset_name in get_preselected_files(),
                            interactive=False,
                        )
                    with gr.Column():
                        download_button = gr.Button(
                            "Download",
                            interactive=asset_name not in get_preselected_files(),
                        )
                    with gr.Column():
                        remove_button = gr.Button(
                            "Remove", interactive=asset_name in get_preselected_files()
                        )

                    checkboxes.append(checkbox)
                    download_buttons.append(download_button)
                    remove_buttons.append(remove_button)

                    def download_click(asset_name=asset_name, checkbox=checkbox):
                        message = download_file(asset_name)
                        is_downloaded, is_not_downloaded = update_buttons(asset_name)
                        return (
                            message,
                            gr.update(interactive=is_not_downloaded),
                            gr.update(interactive=is_downloaded),
                            gr.update(value=True),
                        )

                    def remove_click(asset_name=asset_name, checkbox=checkbox):
                        message = remove_file(asset_name)
                        is_downloaded, is_not_downloaded = update_buttons(asset_name)
                        return (
                            message,
                            gr.update(interactive=is_not_downloaded),
                            gr.update(interactive=is_downloaded),
                            gr.update(value=False),
                        )

                    download_button.click(
                        download_click,
                        outputs=[output_box, download_button, remove_button, checkbox],
                    )
                    remove_button.click(
                        remove_click,
                        outputs=[output_box, download_button, remove_button, checkbox],
                    )

        # Model Inference
        with gr.TabItem("Generate Speech"):
            with gr.Row():
                sid0 = gr.Dropdown("Inferencing voice:", choices=sorted(names))
                with gr.Column():
                    refresh_button = gr.Button(
                        "Refresh voice list and index path", variant="primary"
                    )
                    clean_button = gr.Button(
                        "Unload voice to save GPU memory:", variant="primary"
                    )
                spk_item = gr.Slider(
                    minimum=0,
                    maximum=2333,
                    step=1,
                    label="Select Speaker/Singer ID:",
                    value=0,
                    visible=False,
                    interactive=True,
                )
                clean_button.click(
                    fn=clean, inputs=[], outputs=[sid0], api_name="infer_clean"
                )
            with gr.TabItem("Single Inference"):
                with gr.Group():
                    with gr.Row():
                        with gr.Column():
                            vc_transform0 = gr.Number(
                                label="Octave change (integer, number of semitones, 12 for an octave, -12 for an octave down)",
                                value=0,
                            )
                            input_audio0 = gr.Textbox(
                                label="Input audio file path",
                                placeholder="C:\\Users\\Desktop\\example.mp3",
                            )
                            file_index1 = gr.Textbox(
                                label="Feature index file path (optional)",
                                placeholder="C:\\Users\\Desktop\\model_example.index",
                                interactive=True,
                            )
                            file_index2 = gr.Dropdown(
                                label="Select feature index file path",
                                choices=sorted(index_paths),
                                interactive=True,
                            )
                            f0method0 = gr.Radio(
                                label="Select pitch extraction algorithm: pm for speed, harvest for good low pitch, crepe for good pitch, rmvpe for best pitch",
                                choices=(
                                    ["pm", "harvest", "crepe", "rmvpe"]
                                    if config.dml == False
                                    else ["pm", "harvest", "rmvpe"]
                                ),
                                value="rmvpe",
                                interactive=True,
                            )

                        with gr.Column():
                            resample_sr0 = gr.Slider(
                                minimum=0,
                                maximum=48000,
                                label="Resample SR, 0 for no resample",
                                value=0,
                                step=1,
                                interactive=True,
                            )
                            rms_mix_rate0 = gr.Slider(
                                minimum=0,
                                maximum=1,
                                label="Adjust the volume of the output audio",
                                value=0.25,
                                interactive=True,
                            )
                            protect0 = gr.Slider(
                                minimum=0,
                                maximum=0.5,
                                label="Protect the volume envelope of the output audio (0.5 for no protection)",
                                value=0.33,
                                step=0.01,
                                interactive=True,
                            )
                            filter_radius0 = gr.Slider(
                                minimum=0,
                                maximum=7,
                                label="Filter harvest pitch, 0 for no filter",
                                value=3,
                                step=1,
                                interactive=True,
                            )
                            index_rate1 = gr.Slider(
                                minimum=0,
                                maximum=1,
                                label="Control accent strength, 0 for no accent, 1 for full accent",
                                value=0.75,
                                interactive=True,
                            )
                            f0_file = gr.File(
                                label="Upload F0 file (optional)",
                                visible=False,
                            )

                            refresh_button.click(
                                fn=change_choices,
                                inputs=[],
                                outputs=[sid0, file_index2],
                                api_name="infer_refresh",
                            )

                with gr.Group():
                    with gr.Column():
                        but0 = gr.Button(value="Convert", variant="primary")
                        with gr.Row():
                            vc_output1 = gr.Textbox(label="Output Information")
                            vc_output2 = gr.Audio(label="Output Audio")
                        but0.click(
                            vc.vc_single,
                            [
                                spk_item,
                                input_audio0,
                                vc_transform0,
                                f0_file,
                                f0method0,
                                file_index1,
                                file_index2,
                                index_rate1,
                                filter_radius0,
                                resample_sr0,
                                rms_mix_rate0,
                                protect0,
                            ],
                            [vc_output1, vc_output2],
                            api_name="infer_convert",
                        )

            with gr.TabItem("Batch Inference"):
                gr.Markdown(
                    value="Batch conversion, enter the folder of audio files to be converted, or upload multiple audio files, and output the converted audio in the specified folder (default: 'opt')."
                )
                with gr.Row():
                    with gr.Column():
                        vc_transform1 = gr.Number(
                            label="Pitch shift (integer, number of semitones, 12 for up an octave, -12 for down an octave)",
                            value=0,
                        )
                        opt_input = gr.Textbox(label="Specify output folder", value="opt")
                        file_index3 = gr.Textbox(
                            label="Feature index file path (optional)",
                            value="",
                            interactive=True,
                        )
                        file_index4 = gr.Dropdown(
                            label="Auto-detect index path and select from the dropdown",
                            choices=sorted(index_paths),
                            interactive=True,
                        )
                        f0method1 = gr.Radio(
                            label="Select the pitch extraction algorithm: pm for speed, harvest for good low pitch but extremely slow, crepe for good pitch but GPU-intensive, rmvpe for best pitch with slight GPU usage",
                            choices=(
                                ["pm", "harvest", "crepe", "rmvpe"]
                                if config.dml == False
                                else ["pm", "harvest", "rmvpe"]
                            ),
                            value="rmvpe",
                            interactive=True,
                        )
                        format1 = gr.Radio(
                            label="Export file format",
                            choices=["wav", "flac", "mp3", "m4a"],
                            value="wav",
                            interactive=True,
                        )

                        refresh_button.click(
                            fn=lambda: change_choices()[1],
                            inputs=[],
                            outputs=file_index4,
                            api_name="infer_refresh_batch",
                        )

                    with gr.Column():
                        resample_sr1 = gr.Slider(
                            minimum=0,
                            maximum=48000,
                            label="Post-process resampling to the final sampling rate, 0 for no resampling",
                            value=0,
                            step=1,
                            interactive=True,
                        )
                        rms_mix_rate1 = gr.Slider(
                            minimum=0,
                            maximum=1,
                            label="Adjust the volume envelope scaling",
                            value=1,
                            interactive=True,
                        )
                        protect1 = gr.Slider(
                            minimum=0,
                            maximum=0.5,
                            label="Protect clean consonants and breathing sounds to prevent artifacts like electronic tearing. Maxing out at 0.5 turns it off. Lower values increase protection but may reduce indexing effectiveness",
                            value=0.33,
                            step=0.01,
                            interactive=True,
                        )
                        filter_radius1 = gr.Slider(
                            minimum=0,
                            maximum=7,
                            label=">=3 uses median filtering on the harvest pitch recognition results, the value is the filter radius, using it can weaken the muted sound",
                            value=3,
                            step=1,
                            interactive=True,
                        )
                        index_rate2 = gr.Slider(
                            minimum=0,
                            maximum=1,
                            label="Search feature ratio (controls accent strength, too high has artifacting)",
                            value=1,
                            interactive=True,
                        )
                with gr.Row():
                    dir_input = gr.Textbox(
                        label="Enter the path of the audio folder to be processed (copy it from the address bar of the file manager)",
                        placeholder="C:\\Users\\Desktop\\input_vocal_dir",
                    )
                    inputs = gr.File(
                        file_count="multiple",
                        label="You can also batch input audio files, choose one of the two, folder input takes priority",
                    )

                with gr.Row():
                    but1 = gr.Button("Convert", variant="primary")
                    vc_output3 = gr.Textbox(label="Output information")

                    but1.click(
                        vc.vc_multi,
                        [
                            spk_item,
                            dir_input,
                            opt_input,
                            inputs,
                            vc_transform1,
                            f0method1,
                            file_index3,
                            file_index4,
                            index_rate2,
                            filter_radius1,
                            resample_sr1,
                            rms_mix_rate1,
                            protect1,
                            format1,
                        ],
                        [vc_output3],
                        api_name="infer_convert_batch",
                    )
                sid0.change(
                    fn=vc.get_vc,
                    inputs=[sid0, protect0, protect1],
                    outputs=[spk_item, protect0, protect1, file_index2, file_index4],
                    api_name="infer_change_voice",
                )

        with gr.TabItem("Extract Voice"):
            with gr.Group():
                gr.Markdown(
                    value="Batch processing for vocal accompaniment separation using the UVR5 model.<br>Example of a valid folder path format: D:\\path\\to\\input\\folder (copy it from the file manager address bar).<br>The model is divided into three categories:<br>1. Preserve vocals: Choose this option for audio without harmonies. It preserves vocals better than HP5. It includes two built-in models: HP2 and HP3. HP3 may slightly leak accompaniment but preserves vocals slightly better than HP2.<br>2. Preserve main vocals only: Choose this option for audio with harmonies. It may weaken the main vocals. It includes one built-in model: HP5.<br>3. De-reverb and de-delay models (by FoxJoy):<br>  (1) MDX-Net: The best choice for stereo reverb removal but cannot remove mono reverb;<br>&emsp;(234) DeEcho: Removes delay effects. Aggressive mode removes more thoroughly than Normal mode. DeReverb additionally removes reverb and can remove mono reverb, but not very effectively for heavily reverberated high-frequency content.<br>De-reverb/de-delay notes:<br>1. The processing time for the DeEcho-DeReverb model is approximately twice as long as the other two DeEcho models.<br>2. The MDX-Net-Dereverb model is quite slow.<br>3. The recommended cleanest configuration is to apply MDX-Net first and then DeEcho-Aggressive."
                )
                with gr.Row():
                    with gr.Column():
                        dir_wav_input = gr.Textbox(
                            label="Enter the path of the audio folder to be processed",
                            placeholder="C:\\Users\\Desktop\\todo-songs",
                        )
                        wav_inputs = gr.File(
                            file_count="multiple",
                            label="You can also batch input audio files, choose one of the two, folder input takes priority",
                        )
                    with gr.Column():
                        model_choose = gr.Dropdown(label="Model", choices=uvr5_names)
                        agg = gr.Slider(
                            minimum=0,
                            maximum=20,
                            step=1,
                            label="Degree of vocal extraction aggressiveness",
                            value=10,
                            interactive=True,
                            visible=False,  # Not adjustable for now
                        )
                        opt_vocal_root = gr.Textbox(
                            label="Specify the output folder for vocals", value="opt"
                        )
                        opt_ins_root = gr.Textbox(
                            label="Specify the output folder for accompaniment",
                            value="opt",
                        )
                        format0 = gr.Radio(
                            label="Output file format",
                            choices=["wav", "flac", "mp3", "m4a"],
                            value="flac",
                            interactive=True,
                        )
                    but2 = gr.Button("Convert", variant="primary")
                    vc_output4 = gr.Textbox(label="Output information")
                    but2.click(
                        uvr,
                        [
                            model_choose,
                            dir_wav_input,
                            opt_vocal_root,
                            wav_inputs,
                            opt_ins_root,
                            agg,
                            format0,
                        ],
                        [vc_output4],
                        api_name="uvr_convert",
                    )

        with gr.TabItem("Voice Cloning"):
            gr.Markdown(
                value="Step 1: Fill in the experimental configuration. Experimental data is stored in the 'logs' folder, with each experiment having a separate folder. Manually enter the experiment name path, which contains the experimental configuration, logs, and trained model files."
            )
            with gr.Row():
                exp_dir1 = gr.Textbox(
                    label="Enter the experiment name", value="mi-test"
                )
                sr2 = gr.Radio(
                    label="Target sample rate",
                    choices=["40k", "48k"],
                    value="40k",
                    interactive=True,
                )
                if_f0_3 = gr.Radio(
                    label="Whether the model has pitch guidance (required for singing, optional for speech)",
                    choices=[True, False],
                    value=True,
                    interactive=True,
                )
                version19 = gr.Radio(
                    label="Version",
                    choices=["v1", "v2"],
                    value="v2",
                    interactive=True,
                    visible=True,
                )
                np7 = gr.Slider(
                    minimum=0,
                    maximum=config.n_cpu,
                    step=1,
                    label="Number of CPU processes used for pitch extraction and data processing",
                    value=int(np.ceil(config.n_cpu / 1.5)),
                    interactive=True,
                )
            with gr.Group():  # Temporarily single-person, later support up to 4 people
                gr.Markdown(
                    value="Step 2a: Automatically traverse all files in the training folder that can be decoded into audio and perform slice normalization. Generates 2 wav folders in the experiment directory. Currently, only single-singer/speaker training is supported."
                )
                with gr.Row():
                    trainset_dir4 = gr.Textbox(
                        label="Enter the path of the training folder",
                        value="E:\\Speech Audio+Annotation\\Kenshi Yonezu\\src",
                    )
                    spk_id5 = gr.Slider(
                        minimum=0,
                        maximum=4,
                        step=1,
                        label="Please specify the speaker/singer ID",
                        value=0,
                        interactive=True,
                    )
                    but1 = gr.Button("Process data", variant="primary")
                    info1 = gr.Textbox(label="Output information", value="")
                    but1.click(
                        preprocess_dataset,
                        [trainset_dir4, exp_dir1, sr2, np7],
                        [info1],
                        api_name="train_preprocess",
                    )
            with gr.Group():
                gr.Markdown(
                    value="Step 2b: Use CPU to extract pitch (if the model has pitch), use GPU to extract features (select GPU index)"
                )
                with gr.Row():
                    with gr.Column():
                        gpus6 = gr.Textbox(
                            label="Enter the GPU index(es) separated by '-', e.g., 0-1-2 to use GPU 0, 1, and 2",
                            value=gpus,
                            interactive=True,
                            visible=F0GPUVisible,
                        )
                        gpu_info9 = gr.Textbox(
                            label="GPU information",
                            value=gpu_info,
                            visible=F0GPUVisible,
                        )
                    with gr.Column():
                        f0method8 = gr.Radio(
                            label="Select the pitch extraction algorithm: when extracting singing, you can use 'pm' to speed up. For high-quality speech with fast performance, but worse CPU usage, you can use 'dio'. 'harvest' results in better quality but is slower. 'rmvpe' has the best results and consumes less CPU/GPU",
                            choices=["pm", "harvest", "dio", "rmvpe", "rmvpe_gpu"],
                            value="rmvpe_gpu",
                            interactive=True,
                        )
                        gpus_rmvpe = gr.Textbox(
                            label="Enter the GPU index(es) separated by '-', e.g., 0-0-1 to use 2 processes in GPU0 and 1 process in GPU1",
                            value="%s-%s" % (gpus, gpus),
                            interactive=True,
                            visible=F0GPUVisible,
                        )
                    but2 = gr.Button("Feature extraction", variant="primary")
                    info2 = gr.Textbox(
                        label="Output information", value="", max_lines=8
                    )
                    f0method8.change(
                        fn=change_f0_method,
                        inputs=[f0method8],
                        outputs=[gpus_rmvpe],
                    )
                    but2.click(
                        extract_f0_feature,
                        [
                            gpus6,
                            np7,
                            f0method8,
                            if_f0_3,
                            exp_dir1,
                            version19,
                            gpus_rmvpe,
                        ],
                        [info2],
                        api_name="train_extract_f0_feature",
                    )
            with gr.Group():
                gr.Markdown(
                    value="Step 3: Fill in the training settings and start training the model and index"
                )
                with gr.Row():
                    save_epoch10 = gr.Slider(
                        minimum=1,
                        maximum=50,
                        step=1,
                        label="Save frequency (save_every_epoch)",
                        value=5,
                        interactive=True,
                    )
                    total_epoch11 = gr.Slider(
                        minimum=2,
                        maximum=1000,
                        step=1,
                        label="Total training epochs (total_epoch)",
                        value=20,
                        interactive=True,
                    )
                    batch_size12 = gr.Slider(
                        minimum=1,
                        maximum=40,
                        step=1,
                        label="Batch size per GPU",
                        value=default_batch_size,
                        interactive=True,
                    )
                    if_save_latest13 = gr.Radio(
                        label="Save only the latest ckpt file to save disk space",
                        choices=["Yes", "No"],
                        value="No",
                        interactive=True,
                    )
                    if_cache_gpu17 = gr.Radio(
                        label="Cache all training sets to GPU memory. Caching small datasets (less than 10 minutes) can speed up training, but caching large datasets will consume a lot of GPU memory and may not provide much speed improvement",
                        choices=["Yes", "No"],
                        value="No",
                        interactive=True,
                    )
                    if_save_every_weights18 = gr.Radio(
                        label="Save a small final model to the 'weights' folder at each save point",
                        choices=["Yes", "No"],
                        value="No",
                        interactive=True,
                    )
                with gr.Row():
                    pretrained_G14 = gr.Textbox(
                        label="Load pretrained base model G path",
                        value="assets/pretrained_v2/f0G40k.pth",
                        interactive=True,
                    )
                    pretrained_D15 = gr.Textbox(
                        label="Load pretrained base model D path",
                        value="assets/pretrained_v2/f0D40k.pth",
                        interactive=True,
                    )
                    sr2.change(
                        change_sr2,
                        [sr2, if_f0_3, version19],
                        [pretrained_G14, pretrained_D15],
                    )
                    version19.change(
                        change_version19,
                        [sr2, if_f0_3, version19],
                        [pretrained_G14, pretrained_D15, sr2],
                    )
                    if_f0_3.change(
                        change_f0,
                        [if_f0_3, sr2, version19],
                        [f0method8, gpus_rmvpe, pretrained_G14, pretrained_D15],
                    )
                    gpus16 = gr.Textbox(
                        label="Enter the GPU index(es) separated by '-', e.g., 0-1-2 to use GPU 0, 1, and 2",
                        value=gpus,
                        interactive=True,
                    )
                    but3 = gr.Button("Train model", variant="primary")
                    but4 = gr.Button("Train feature index", variant="primary")
                    but5 = gr.Button("One-click training", variant="primary")
                    info3 = gr.Textbox(
                        label="Output information", value="", max_lines=10
                    )
                    but3.click(
                        click_train,
                        [
                            exp_dir1,
                            sr2,
                            if_f0_3,
                            spk_id5,
                            save_epoch10,
                            total_epoch11,
                            batch_size12,
                            if_save_latest13,
                            pretrained_G14,
                            pretrained_D15,
                            gpus16,
                            if_cache_gpu17,
                            if_save_every_weights18,
                            version19,
                        ],
                        info3,
                        api_name="train_start",
                    )
                    but4.click(train_index, [exp_dir1, version19], info3)
                    but5.click(
                        train1key,
                        [
                            exp_dir1,
                            sr2,
                            if_f0_3,
                            trainset_dir4,
                            spk_id5,
                            np7,
                            f0method8,
                            save_epoch10,
                            total_epoch11,
                            batch_size12,
                            if_save_latest13,
                            pretrained_G14,
                            pretrained_D15,
                            gpus16,
                            if_cache_gpu17,
                            if_save_every_weights18,
                            version19,
                            gpus_rmvpe,
                        ],
                        info3,
                        api_name="train_start_all",
                    )

        with gr.TabItem("Checkpoints"):
            with gr.Group():
                gr.Markdown(value="Model fusion, can be used to test timbre fusion")
                with gr.Row():
                    ckpt_a = gr.Textbox(
                        label="Path to Model A", value="", interactive=True
                    )
                    ckpt_b = gr.Textbox(
                        label="Path to Model B", value="", interactive=True
                    )
                    alpha_a = gr.Slider(
                        minimum=0,
                        maximum=1,
                        label="Weight (w) for Model A",
                        value=0.5,
                        interactive=True,
                    )
                with gr.Row():
                    sr_ = gr.Radio(
                        label="Target sample rate",
                        choices=["40k", "48k"],
                        value="40k",
                        interactive=True,
                    )
                    if_f0_ = gr.Radio(
                        label="Whether the model has pitch guidance",
                        choices=["Yes", "No"],
                        value="Yes",
                        interactive=True,
                    )
                    info__ = gr.Textbox(
                        label="Model information to be placed",
                        value="",
                        max_lines=8,
                        interactive=True,
                    )
                    name_to_save0 = gr.Textbox(
                        label="Saved model name (without extension)",
                        value="",
                        max_lines=1,
                        interactive=True,
                    )
                    version_2 = gr.Radio(
                        label="Model architecture version",
                        choices=["v1", "v2"],
                        value="v1",
                        interactive=True,
                    )
                with gr.Row():
                    but6 = gr.Button("Fusion", variant="primary")
                    info4 = gr.Textbox(
                        label="Output information", value="", max_lines=8
                    )
                but6.click(
                    merge,
                    [
                        ckpt_a,
                        ckpt_b,
                        alpha_a,
                        sr_,
                        if_f0_,
                        info__,
                        name_to_save0,
                        version_2,
                    ],
                    info4,
                    api_name="ckpt_merge",
                )  # def merge(path1,path2,alpha1,sr,f0,info):
            with gr.Group():
                gr.Markdown(
                    value="Modify model information (only supported for small model files extracted from the 'weights' folder)"
                )
                with gr.Row():
                    ckpt_path0 = gr.Textbox(
                        label="Path to Model", value="", interactive=True
                    )
                    info_ = gr.Textbox(
                        label="Model information to be modified",
                        value="",
                        max_lines=8,
                        interactive=True,
                    )
                    name_to_save1 = gr.Textbox(
                        label="Save file name (default: same as the source file)",
                        value="",
                        max_lines=8,
                        interactive=True,
                    )
                with gr.Row():
                    but7 = gr.Button("Modify", variant="primary")
                    info5 = gr.Textbox(
                        label="Output information", value="", max_lines=8
                    )
                but7.click(
                    change_info,
                    [ckpt_path0, info_, name_to_save1],
                    info5,
                    api_name="ckpt_modify",
                )
            with gr.Group():
                gr.Markdown(
                    value="View model information (only supported for small model files extracted from the 'weights' folder)"
                )
                with gr.Row():
                    ckpt_path1 = gr.Textbox(
                        label="Path to Model", value="", interactive=True
                    )
                    but8 = gr.Button("View", variant="primary")
                    info6 = gr.Textbox(
                        label="Output information", value="", max_lines=8
                    )
                but8.click(show_info, [ckpt_path1], info6, api_name="ckpt_show")
            with gr.Group():
                gr.Markdown(
                    value="Model extraction (enter the path of the large file model under the 'logs' folder). This is useful if you want to stop training halfway and manually extract and save a small model file, or if you want to test an intermediate model."
                )
                with gr.Row():
                    ckpt_path2 = gr.Textbox(
                        label="Path to Model",
                        value="E:\\codes\\py39\\logs\\mi-test_f0_48k\\G_23333.pth",
                        interactive=True,
                    )
                    save_name = gr.Textbox(
                        label="Save name", value="", interactive=True
                    )
                    sr__ = gr.Radio(
                        label="Target sample rate",
                        choices=["32k", "40k", "48k"],
                        value="40k",
                        interactive=True,
                    )
                    if_f0__ = gr.Radio(
                        label="Whether the model has pitch guidance (1: yes, 0: no)",
                        choices=["1", "0"],
                        value="1",
                        interactive=True,
                    )
                    version_1 = gr.Radio(
                        label="Model architecture version",
                        choices=["v1", "v2"],
                        value="v2",
                        interactive=True,
                    )
                    info___ = gr.Textbox(
                        label="Model information to be placed",
                        value="",
                        max_lines=8,
                        interactive=True,
                    )
                    but9 = gr.Button("Extract", variant="primary")
                    info7 = gr.Textbox(
                        label="Output information", value="", max_lines=8
                    )
                    ckpt_path2.change(
                        change_info_, [ckpt_path2], [sr__, if_f0__, version_1]
                    )
                but9.click(
                    extract_small_model,
                    [ckpt_path2, save_name, sr__, if_f0__, info___, version_1],
                    info7,
                    api_name="ckpt_extract",
                )

        with gr.TabItem("Onnx Export"):
            with gr.Row():
                ckpt_dir = gr.Textbox(
                    label="RVC Model Path", value="", interactive=True
                )
            with gr.Row():
                onnx_dir = gr.Textbox(
                    label="Onnx Output Path", value="", interactive=True
                )
            with gr.Row():
                infoOnnx = gr.Label(label="info")
            with gr.Row():
                butOnnx = gr.Button("Export Onnx Model", variant="primary")
            butOnnx.click(
                export_onnx, [ckpt_dir, onnx_dir], infoOnnx, api_name="export_onnx"
            )

    app.launch(
        server_name="0.0.0.0",
        inbrowser=not config.noautoopen,
        server_port=config.listen_port,
        # quiet=True,
        show_error=True,
    )
