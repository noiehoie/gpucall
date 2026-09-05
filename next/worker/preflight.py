"""Build-time CPU contract check; fails before any GPU is rented."""
from transformers import AutoConfig, AutoProcessor, AutoTokenizer
from PIL import Image

path = "/opt/gpucall-model"
options = {"local_files_only": True, "trust_remote_code": False}
config = AutoConfig.from_pretrained(path, **options)
if config.model_type == "qwen2_5_vl":
    processor = AutoProcessor.from_pretrained(path, **options)
    text = "<|im_start|>user\n<|vision_start|><|image_pad|><|vision_end|>What color?<|im_end|>\n<|im_start|>assistant\n"
    encoded = processor(text=[text], images=[Image.new("RGB", (256, 256), (0, 0, 255))], return_tensors="pt")
    assert encoded.input_ids.shape[1] > 0 and encoded.pixel_values.numel() > 0
else:
    tokenizer = AutoTokenizer.from_pretrained(path, **options)
    assert tokenizer("GPUCALL_OK", return_tensors="pt").input_ids.shape[1] > 0
print("CPU preflight passed:", config.model_type)
