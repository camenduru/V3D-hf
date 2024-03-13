# TODO
import numpy as np
import argparse
import torch
from torchvision.utils import make_grid
import tempfile
import gradio as gr
from omegaconf import OmegaConf
from einops import rearrange
from scripts.pub.V3D_512 import (
    sample_one,
    get_batch,
    get_unique_embedder_keys_from_conditioner,
    load_model,
)
from sgm.util import default, instantiate_from_config
from safetensors.torch import load_file as load_safetensors
from PIL import Image
from kiui.op import recenter
from torchvision.transforms import ToTensor
from einops import rearrange, repeat
import rembg
import os
from glob import glob
from mediapy import write_video
from pathlib import Path
import spaces
from huggingface_hub import hf_hub_download
import imageio
import cv2


@spaces.GPU
def do_sample(
    image,
    num_frames,
    num_steps,
    decoding_t,
    border_ratio,
    ignore_alpha,
    output_folder,
):
    # if image.mode == "RGBA":
    #     image = image.convert("RGB")
    image = Image.fromarray(image)
    w, h = image.size

    if border_ratio > 0:
        if image.mode != "RGBA" or ignore_alpha:
            image = image.convert("RGB")
            image = np.asarray(image)
            carved_image = rembg.remove(image, session=rembg_session)  # [H, W, 4]
        else:
            image = np.asarray(image)
            carved_image = image
        mask = carved_image[..., -1] > 0
        image = recenter(carved_image, mask, border_ratio=border_ratio)
        image = image.astype(np.float32) / 255.0
        if image.shape[-1] == 4:
            image = image[..., :3] * image[..., 3:4] + (1 - image[..., 3:4])
        image = Image.fromarray((image * 255).astype(np.uint8))
    else:
        print("Ignore border ratio")
    image = image.resize((512, 512))

    image = ToTensor()(image)
    image = image * 2.0 - 1.0

    image = image.unsqueeze(0).to(device)
    H, W = image.shape[2:]
    assert image.shape[1] == 3
    F = 8
    C = 4
    shape = (num_frames, C, H // F, W // F)

    value_dict = {}
    value_dict["motion_bucket_id"] = 0
    value_dict["fps_id"] = 0
    value_dict["cond_aug"] = 0.05
    value_dict["cond_frames_without_noise"] = clip_model(image)
    value_dict["cond_frames"] = ae_model.encode(image)
    value_dict["cond_frames"] += 0.05 * torch.randn_like(value_dict["cond_frames"])
    value_dict["cond_aug"] = 0.05

    print(device)
    with torch.no_grad():
        with torch.autocast(device_type="cuda"):
            batch, batch_uc = get_batch(
                get_unique_embedder_keys_from_conditioner(model.conditioner),
                value_dict,
                [1, num_frames],
                T=num_frames,
                device=device,
            )
            c, uc = model.conditioner.get_unconditional_conditioning(
                batch,
                batch_uc=batch_uc,
                force_uc_zero_embeddings=[
                    "cond_frames",
                    "cond_frames_without_noise",
                ],
            )

            for k in ["crossattn", "concat"]:
                uc[k] = repeat(uc[k], "b ... -> b t ...", t=num_frames)
                uc[k] = rearrange(uc[k], "b t ... -> (b t) ...", t=num_frames)
                c[k] = repeat(c[k], "b ... -> b t ...", t=num_frames)
                c[k] = rearrange(c[k], "b t ... -> (b t) ...", t=num_frames)

            randn = torch.randn(shape, device=device)
            randn = randn.to(device)

            additional_model_inputs = {}
            additional_model_inputs["image_only_indicator"] = torch.zeros(
                2, num_frames
            ).to(device)
            additional_model_inputs["num_video_frames"] = batch["num_video_frames"]

            def denoiser(input, sigma, c):
                return model.denoiser(
                    model.model, input, sigma, c, **additional_model_inputs
                )

            samples_z = model.sampler(denoiser, randn, cond=c, uc=uc)
            model.en_and_decode_n_samples_a_time = decoding_t
            samples_x = model.decode_first_stage(samples_z)
            samples = torch.clamp((samples_x + 1.0) / 2.0, min=0.0, max=1.0)

            os.makedirs(output_folder, exist_ok=True)
            base_count = len(glob(os.path.join(output_folder, "*.mp4")))
            video_path = os.path.join(output_folder, f"{base_count:06d}.mp4")

            frames = (
                (rearrange(samples, "t c h w -> t h w c") * 255)
                .cpu()
                .numpy()
                .astype(np.uint8)
            )
            # write_video(video_path, frames, fps=6)
            # writer = cv2.VideoWriter(
            #     video_path,
            #     cv2.VideoWriter_fourcc(*"MP4V"),
            #     6,
            #     (frames.shape[-1], frames.shape[-2]),
            # )
            # for fr in frames:
            #     writer.write(cv2.cvtColor(fr, cv2.COLOR_RGB2BGR))
            # writer.release()
            imageio.mimwrite(video_path, frames)

    return video_path


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# download
V3D_ckpt_path = hf_hub_download(repo_id="heheyas/V3D", filename="V3D.ckpt")
svd_xt_ckpt_path = hf_hub_download(
    repo_id="stabilityai/stable-video-diffusion-img2vid-xt",
    filename="svd_xt.safetensors",
)

model_config = "./scripts/pub/configs/V3D_512.yaml"
num_frames = OmegaConf.load(
    model_config
).model.params.sampler_config.params.guider_config.params.num_frames
print("Detected num_frames:", num_frames)
# num_steps = default(num_steps, 25)
num_steps = 25
output_folder = "outputs/V3D_512"

sd = load_safetensors(svd_xt_ckpt_path)
clip_model_config = OmegaConf.load("./configs/embedder/clip_image.yaml")
clip_model = instantiate_from_config(clip_model_config).eval()
clip_sd = dict()
for k, v in sd.items():
    if "conditioner.embedders.0" in k:
        clip_sd[k.replace("conditioner.embedders.0.", "")] = v
clip_model.load_state_dict(clip_sd)
clip_model = clip_model.to(device)

ae_model_config = OmegaConf.load("./configs/ae/video.yaml")
ae_model = instantiate_from_config(ae_model_config).eval()
encoder_sd = dict()
for k, v in sd.items():
    if "first_stage_model" in k:
        encoder_sd[k.replace("first_stage_model.", "")] = v
ae_model.load_state_dict(encoder_sd)
ae_model = ae_model.to(device)
rembg_session = rembg.new_session()

model, _ = load_model(
    model_config,
    device,
    num_frames,
    num_steps,
    min_cfg=3.5,
    max_cfg=3.5,
    ckpt_path=V3D_ckpt_path,
)
model = model.to(device)

with gr.Blocks(title="V3D", theme=gr.themes.Monochrome()) as demo:
    with gr.Row(equal_height=True):
        with gr.Column():
            input_image = gr.Image(value=None, label="Input Image")

            border_ratio_slider = gr.Slider(
                value=0.3,
                label="Border Ratio",
                minimum=0.05,
                maximum=0.5,
                step=0.05,
            )
            decoding_t_slider = gr.Slider(
                value=1,
                label="Number of Decoding frames",
                minimum=1,
                maximum=num_frames,
                step=1,
            )
            min_guidance_slider = gr.Slider(
                value=3.5,
                label="Min CFG Value",
                minimum=0.05,
                maximum=0.5,
                step=0.05,
            )
            max_guidance_slider = gr.Slider(
                value=3.5,
                label="Max CFG Value",
                minimum=0.05,
                maximum=0.5,
                step=0.05,
            )
            run_button = gr.Button(value="Run V3D")

        with gr.Column():
            output_video = gr.Video(value=None, label="Output Orbit Video")

    @run_button.click(
        inputs=[
            input_image,
            border_ratio_slider,
            min_guidance_slider,
            max_guidance_slider,
            decoding_t_slider,
        ],
        outputs=[output_video],
    )
    def _(image, border_ratio, min_guidance, max_guidance, decoding_t):
        model.sampler.guider.max_scale = max_guidance
        model.sampler.guider.min_scale = min_guidance
        return do_sample(
            image,
            num_frames,
            num_steps,
            int(decoding_t),
            border_ratio,
            False,
            output_folder,
        )


demo.launch()
