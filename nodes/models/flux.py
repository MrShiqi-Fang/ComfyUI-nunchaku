import json
import os
from contextlib import nullcontext

import torch
from diffusers import FluxPipeline, FluxTransformer2DModel
from einops import rearrange
from torch import nn

import comfy.model_patcher
import folder_paths
from comfy.ldm.common_dit import pad_to_patch_size
from comfy.supported_models import Flux, FluxSchnell
from nunchaku import NunchakuFluxTransformer2dModel
from nunchaku.caching.diffusers_adapters.flux import apply_cache_on_transformer
from nunchaku.caching.utils import cache_context, create_cache_context
from nunchaku.lora.flux.compose import compose_lora
from nunchaku.utils import load_state_dict_in_safetensors


class ComfyFluxWrapper(nn.Module):
    def __init__(self, model: NunchakuFluxTransformer2dModel, config):
        super(ComfyFluxWrapper, self).__init__()
        self.model = model
        self.dtype = next(model.parameters()).dtype
        self.config = config
        self.loras = []

        self._prev_timestep = None  # for first-block cache
        self._cache_context = None

    def forward(self, x, timestep, context, y, guidance, control=None, transformer_options={}, **kwargs):
        assert control is None  # for now

        model = self.model
        assert isinstance(model, NunchakuFluxTransformer2dModel)

        bs, c, h, w = x.shape
        patch_size = self.config["patch_size"]
        x = pad_to_patch_size(x, (patch_size, patch_size))

        img = rearrange(x, "b c (h ph) (w pw) -> b (h w) (c ph pw)", ph=patch_size, pw=patch_size)

        h_len = (h + (patch_size // 2)) // patch_size
        w_len = (w + (patch_size // 2)) // patch_size
        img_ids = FluxPipeline._prepare_latent_image_ids(bs, h_len, w_len, x.device, x.dtype)
        txt_ids = torch.zeros((context.shape[1], 3), device=x.device, dtype=x.dtype)

        # load and compose lora
        if self.loras != model.comfy_lora_meta_list:
            lora_to_be_composed = []
            for _ in range(max(0, len(model.comfy_lora_meta_list) - len(self.loras))):
                model.comfy_lora_meta_list.pop()
                model.comfy_lora_sd_list.pop()
            for i in range(len(self.loras)):
                meta = self.loras[i]
                if i >= len(model.comfy_lora_meta_list):
                    sd = load_state_dict_in_safetensors(meta[0])
                    model.comfy_lora_meta_list.append(meta)
                    model.comfy_lora_sd_list.append(sd)
                elif model.comfy_lora_meta_list[i] != meta:
                    if meta[0] != model.comfy_lora_meta_list[i][0]:
                        sd = load_state_dict_in_safetensors(meta[0])
                        model.comfy_lora_sd_list[i] = sd
                    model.comfy_lora_meta_list[i] = meta
                lora_to_be_composed.append(({k: v for k, v in model.comfy_lora_sd_list[i].items()}, meta[1]))

            composed_lora = compose_lora(lora_to_be_composed)

            if len(composed_lora) == 0:
                model.reset_lora()
            else:
                if "x_embedder.lora_A.weight" in composed_lora:
                    new_in_channels = composed_lora["x_embedder.lora_A.weight"].shape[1]
                    current_in_channels = model.x_embedder.in_features
                    if new_in_channels < current_in_channels:
                        model.reset_x_embedder()
                model.update_lora_params(composed_lora)

        if getattr(model, "_is_cached", False):
            if self._prev_timestep is None or self._prev_timestep < timestep:
                self._cache_context = create_cache_context()
            context = cache_context(self._cache_context)
        else:
            context = nullcontext()

        with context:
            out = model(
                hidden_states=img,
                encoder_hidden_states=context,
                pooled_projections=y,
                timestep=timestep,
                img_ids=img_ids,
                txt_ids=txt_ids,
                guidance=guidance if self.config["guidance_embed"] else None,
            ).sample

        out = rearrange(out, "b (h w) (c ph pw) -> b c (h ph) (w pw)", h=h_len, w=w_len, ph=patch_size, pw=patch_size)
        out = out[:, :, :h, :w]

        self._prev_timestep = timestep
        return out


class NunchakuFluxDiTLoader:
    @classmethod
    def INPUT_TYPES(s):
        prefixes = folder_paths.folder_names_and_paths["diffusion_models"][0]
        local_folders = set()
        for prefix in prefixes:
            if os.path.exists(prefix) and os.path.isdir(prefix):
                local_folders_ = os.listdir(prefix)
                local_folders_ = [
                    folder
                    for folder in local_folders_
                    if not folder.startswith(".") and os.path.isdir(os.path.join(prefix, folder))
                ]
                local_folders.update(local_folders_)
        model_paths = sorted(list(local_folders))
        ngpus = torch.cuda.device_count()
        return {
            "required": {
                "model_path": (model_paths, {"tooltip": "The SVDQuant quantized FLUX.1 models."}),
                "cache_threshold": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0,
                        "max": 1,
                        "step": 0.001,
                        "tooltip": "Adjusts the caching tolerance like `residual_diff_threshold` in WaveSpeed. "
                        "Increasing the value enhances speed at the cost of quality. "
                        "A typical setting is 0.12. Setting it to 0 disables the effect.",
                    },
                ),
                "cpu_offload": (
                    ["auto", "enable", "disable"],
                    {
                        "default": "auto",
                        "tooltip": "Whether to enable CPU offload for the transformer model. 'auto' will enable it if the GPU memory is less than 14G.",
                    },
                ),
                "device_id": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": ngpus - 1,
                        "step": 1,
                        "display": "number",
                        "lazy": True,
                        "tooltip": "The GPU device ID to use for the model.",
                    },
                ),
            }
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load_model"
    CATEGORY = "Nunchaku"
    TITLE = "Nunchaku FLUX DiT Loader"

    def load_model(
        self, model_path: str, cache_threshold: float, cpu_offload: str, device_id: int, **kwargs
    ) -> tuple[FluxTransformer2DModel]:
        device = f"cuda:{device_id}"
        prefixes = folder_paths.folder_names_and_paths["diffusion_models"][0]
        for prefix in prefixes:
            if os.path.exists(os.path.join(prefix, model_path)):
                model_path = os.path.join(prefix, model_path)
                break

        # Check if the device_id is valid
        if device_id >= torch.cuda.device_count():
            raise ValueError(f"Invalid device_id: {device_id}. Only {torch.cuda.device_count()} GPUs available.")

        # Get the GPU properties
        gpu_properties = torch.cuda.get_device_properties(device_id)
        gpu_memory = gpu_properties.total_memory / (1024**2)  # Convert to MB
        gpu_name = gpu_properties.name
        print(f"GPU {device_id} ({gpu_name}) Memory: {gpu_memory} MB")

        # Check if CPU offload needs to be enabled
        if cpu_offload == "auto":
            if gpu_memory < 14336:  # 14GB threshold
                cpu_offload_enabled = True
                print("VRAM < 14GiB，enable CPU offload")
            else:
                cpu_offload_enabled = False
                print("VRAM > 14GiB，disable CPU offload")
        elif cpu_offload == "enable":
            cpu_offload_enabled = True
            print("Enable CPU offload")
        else:
            cpu_offload_enabled = False
            print("Disable CPU offload")

        transformer = NunchakuFluxTransformer2dModel.from_pretrained(model_path, offload=cpu_offload_enabled).to(device)
        if cache_threshold > 0:
            transformer = apply_cache_on_transformer(transformer=transformer, residual_diff_threshold=cache_threshold)

        if os.path.exists(os.path.join(model_path, "comfy_config.json")):
            config_path = os.path.join(model_path, "comfy_config.json")
        else:
            default_config_root = os.path.join(os.path.dirname(__file__), "configs")
            config_name = os.path.basename(model_path).replace("svdq-int4-", "").replace("svdq-fp4-", "")
            config_path = os.path.join(default_config_root, f"{config_name}.json")
            assert os.path.exists(config_path), f"Config file not found: {config_path}"

        print(f"Loading configuration from {config_path}")
        comfy_config = json.load(open(config_path, "r"))
        model_class_name = comfy_config["model_class"]
        if model_class_name == "FluxSchnell":
            model_class = FluxSchnell
        else:
            assert model_class_name == "Flux", f"Unknown model class {model_class_name}."
            model_class = Flux
        model_config = model_class(comfy_config["model_config"])
        model_config.set_inference_dtype(torch.bfloat16, None)
        model_config.custom_operations = None
        model = model_config.get_model({})
        model.diffusion_model = ComfyFluxWrapper(transformer, config=comfy_config["model_config"])
        model = comfy.model_patcher.ModelPatcher(model, device, device_id)
        return (model,)
