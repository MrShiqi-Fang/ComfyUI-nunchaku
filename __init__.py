# only import if running as a custom node
from .nodes.lora import NunchakuFluxLoraLoader
from .nodes.models import NunchakuFluxDiTLoader, NunchakuTextEncoderLoader
from .nodes.preprocessors import FluxDepthPreprocessor

NODE_CLASS_MAPPINGS = {
    "SVDQuantFluxDiTLoader": NunchakuFluxDiTLoader,
    "SVDQuantTextEncoderLoader": NunchakuTextEncoderLoader,
    "SVDQuantFluxLoraLoader": NunchakuFluxLoraLoader,
    "SVDQuantDepthPreprocessor": FluxDepthPreprocessor,
}
NODE_DISPLAY_NAME_MAPPINGS = {k: v.TITLE for k, v in NODE_CLASS_MAPPINGS.items()}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
