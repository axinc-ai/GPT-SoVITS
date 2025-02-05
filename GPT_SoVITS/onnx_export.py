from module.models_onnx import SynthesizerTrn, symbols
from AR.models.t2s_lightning_module_onnx import Text2SemanticLightningModule
import torch
import torchaudio
from torch import nn
from feature_extractor import cnhubert

#cnhubert_base_path = "pretrained_models/chinese-hubert-base"

import os
cnhubert_base_path = os.environ.get(
    "cnhubert_base_path", "GPT_SoVITS/pretrained_models/chinese-hubert-base"
)

cnhubert.cnhubert_base_path=cnhubert_base_path
ssl_model = cnhubert.get_model()
from text import cleaned_text_to_sequence
import soundfile
from my_utils import load_audio
import os
import json

debug_dump = True
debug_trace = False # 完全一致のためにtopKを1に固定する

onnx_export = True
onnx_import = False

def spectrogram_torch(y, n_fft, sampling_rate, hop_size, win_size, center=False):
    hann_window = torch.hann_window(win_size).to(
            dtype=y.dtype, device=y.device
        )
    y = torch.nn.functional.pad(
        y.unsqueeze(1),
        (int((n_fft - hop_size) / 2), int((n_fft - hop_size) / 2)),
        mode="reflect",
    )
    y = y.squeeze(1)
    spec = torch.stft(
        y,
        n_fft,
        hop_length=hop_size,
        win_length=win_size,
        window=hann_window,
        center=center,
        pad_mode="reflect",
        normalized=False,
        onesided=True,
        return_complex=False,
    )
    spec = torch.sqrt(spec.pow(2).sum(-1) + 1e-6)
    return spec


class DictToAttrRecursive(dict):
    def __init__(self, input_dict):
        super().__init__(input_dict)
        for key, value in input_dict.items():
            if isinstance(value, dict):
                value = DictToAttrRecursive(value)
            self[key] = value
            setattr(self, key, value)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")

    def __setattr__(self, key, value):
        if isinstance(value, dict):
            value = DictToAttrRecursive(value)
        super(DictToAttrRecursive, self).__setitem__(key, value)
        super().__setattr__(key, value)

    def __delattr__(self, item):
        try:
            del self[item]
        except KeyError:
            raise AttributeError(f"Attribute {item} not found")


class T2SEncoder(nn.Module):
    def __init__(self, t2s, vits):
        super().__init__()
        self.encoder = t2s.onnx_encoder
        self.vits = vits
    
    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content):
        codes = self.vits.extract_latent(ssl_content)
        prompt_semantic = codes[0, 0]
        if debug_dump:
            print("prompt_semantic", prompt_semantic)
        bert = torch.cat([ref_bert.transpose(0, 1), text_bert.transpose(0, 1)], 1)
        all_phoneme_ids = torch.cat([ref_seq, text_seq], 1)
        bert = bert.unsqueeze(0)
        prompt = prompt_semantic.unsqueeze(0)
        return self.encoder(all_phoneme_ids, bert), prompt


class T2SModel(nn.Module):
    def __init__(self, t2s_path, vits_model):
        super().__init__()
        dict_s1 = torch.load(t2s_path, map_location="cpu")
        self.config = dict_s1["config"]
        self.t2s_model = Text2SemanticLightningModule(self.config, "ojbk", is_train=False)
        self.t2s_model.load_state_dict(dict_s1["weight"])
        self.t2s_model.eval()
        self.vits_model = vits_model.vq_model
        self.hz = 50
        self.max_sec = self.config["data"]["max_sec"]
        if debug_trace:
            self.config["inference"]["top_k"] = 1
        self.t2s_model.model.top_k = torch.LongTensor([self.config["inference"]["top_k"]])
        self.t2s_model.model.early_stop_num = torch.LongTensor([self.hz * self.max_sec])
        self.t2s_model = self.t2s_model.model
        self.t2s_model.init_onnx()
        self.onnx_encoder = T2SEncoder(self.t2s_model, self.vits_model)
        self.first_stage_decoder = self.t2s_model.first_stage_decoder
        self.stage_decoder = self.t2s_model.stage_decoder
        #self.t2s_model = torch.jit.script(self.t2s_model)

    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content):
        early_stop_num = self.t2s_model.early_stop_num

        top_k = torch.LongTensor([5])
        if debug_trace:
            top_p = torch.Tensor([1.0])
        else:
            top_p = torch.Tensor([0.95])
        temperature = torch.Tensor([1.0])
        repetition_penalty = torch.Tensor([1.35])

        if debug_dump:
            print(ref_seq)
            print(text_seq)
            print(ref_bert)
            print(text_bert)

        if onnx_import:
            import onnxruntime
            sess_encoder = onnxruntime.InferenceSession(f"onnx/nahida/nahida_t2s_encoder.onnx", providers=["CPU"])
            sess_fsdec = onnxruntime.InferenceSession(f"onnx/nahida/nahida_t2s_fsdec.onnx", providers=["CPU"])
            sess_sdec = onnxruntime.InferenceSession(f"onnx/nahida/nahida_t2s_sdec.onnx", providers=["CPU"])

        #[1,N] [1,N] [N, 1024] [N, 1024] [1, 768, N]
        if onnx_import:
            x, prompts = sess_encoder.run(None, {"ref_seq":ref_seq.detach().numpy(), "text_seq":text_seq.detach().numpy(), "ref_bert":ref_bert.detach().numpy(), "text_bert":text_bert.detach().numpy(), "ssl_content":ssl_content.detach().numpy()})
            x = torch.from_numpy(x)
            prompts = torch.from_numpy(prompts)
        else:
            x, prompts = self.onnx_encoder(ref_seq, text_seq, ref_bert, text_bert, ssl_content)
            if debug_dump:
                print("onnx_encoder x", x)
                print("onnx_encoder prompts", prompts)

        prefix_len = prompts.shape[1]

        #[1,N,512] [1,N]
        if onnx_import:
            y, k, v, y_emb, x_example = sess_fsdec.run(None, {"x":x.detach().numpy(), "prompts":prompts.detach().numpy(), "top_k":top_k.detach().numpy(), "top_p":top_p.detach().numpy(), "temperature":temperature.detach().numpy(), "repetition_penalty":repetition_penalty.detach().numpy()})
            y = torch.from_numpy(y)
            k = torch.from_numpy(k)
            v = torch.from_numpy(v)
            y_emb = torch.from_numpy(y_emb)
            x_example = torch.from_numpy(x_example)
        else:
            y, k, v, y_emb, x_example = self.first_stage_decoder(x, prompts, top_k, top_p, temperature, repetition_penalty)

        stop = False
        for idx in range(1, 1500):
            #[1, N] [N_layer, N, 1, 512] [N_layer, N, 1, 512] [1, N, 512] [1] [1, N, 512] [1, N]
            if onnx_import:
                y, k, v, y_emb, logits, samples = sess_sdec.run(None, {"iy":y.detach().numpy(), "ik":k.detach().numpy(), "iv":v.detach().numpy(), "iy_emb":y_emb.detach().numpy(), "ix_example":x_example.detach().numpy(), "top_k":top_k.detach().numpy(), "top_p":top_p.detach().numpy(), "temperature":temperature.detach().numpy(), "repetition_penalty":repetition_penalty.detach().numpy()})
                y = torch.from_numpy(y)
                k = torch.from_numpy(k)
                v = torch.from_numpy(v)
                y_emb = torch.from_numpy(y_emb)
                logits = torch.from_numpy(logits)
                samples = torch.from_numpy(samples)
            else:
                enco = self.stage_decoder(y, k, v, y_emb, x_example, top_k, top_p, temperature, repetition_penalty)
                y, k, v, y_emb, logits, samples = enco
            if early_stop_num != -1 and (y.shape[1] - prefix_len) > early_stop_num:
                stop = True
            if torch.argmax(logits, dim=-1)[0] == self.t2s_model.EOS or samples[0, 0] == self.t2s_model.EOS:
                stop = True
            if stop:
                print(f"T2S Decoding EOS [{prefix_len} -> {y.shape[1]}]")
                break
        y[0, -1] = 0

        return y[:, -idx:-1].unsqueeze(0) # added -1 for matching torch

    def export(self, ref_seq, text_seq, ref_bert, text_bert, ssl_content, project_name, dynamo=False):
        top_k = torch.LongTensor([5])
        if debug_trace:
            top_p = torch.Tensor([1.0])
        else:
            top_p = torch.Tensor([0.95])
        temperature = torch.Tensor([1.0])
        repetition_penalty = torch.Tensor([1.35])

        #self.onnx_encoder = torch.jit.script(self.onnx_encoder)
        if dynamo:
            export_options = torch.onnx.ExportOptions(dynamic_shapes=True)
            onnx_encoder_export_output = torch.onnx.dynamo_export(
                self.onnx_encoder,
                (ref_seq, text_seq, ref_bert, text_bert, ssl_content),
                export_options=export_options
            )
            onnx_encoder_export_output.save(f"onnx/{project_name}/{project_name}_t2s_encoder.onnx")
            return

        torch.onnx.export(
            self.onnx_encoder,
            (ref_seq, text_seq, ref_bert, text_bert, ssl_content),
            f"onnx/{project_name}/{project_name}_t2s_encoder.onnx",
            input_names=["ref_seq", "text_seq", "ref_bert", "text_bert", "ssl_content"],
            output_names=["x", "prompts"],
            dynamic_axes={
                "ref_seq": {1 : "ref_length"},
                "text_seq": {1 : "text_length"},
                "ref_bert": {0 : "ref_length"},
                "text_bert": {0 : "text_length"},
                "ssl_content": {2 : "ssl_length"},
            },
            opset_version=16
        )
        x, prompts = self.onnx_encoder(ref_seq, text_seq, ref_bert, text_bert, ssl_content)

        torch.onnx.export(
            self.first_stage_decoder,
            (x, prompts, top_k, top_p, temperature, repetition_penalty),
            f"onnx/{project_name}/{project_name}_t2s_fsdec.onnx",
            input_names=["x", "prompts", "top_k", "top_p", "temperature", "repetition_penalty"],
            output_names=["y", "k", "v", "y_emb", "x_example"],
            dynamic_axes={
                "x": {1 : "x_length"},
                "prompts": {1 : "prompts_length"},
            },
            verbose=False,
            opset_version=16
        )
        y, k, v, y_emb, x_example = self.first_stage_decoder(x, prompts, top_k, top_p, temperature, repetition_penalty)

        torch.onnx.export(
            self.stage_decoder,
            (y, k, v, y_emb, x_example, top_k, top_p, temperature, repetition_penalty),
            f"onnx/{project_name}/{project_name}_t2s_sdec.onnx",
            input_names=["iy", "ik", "iv", "iy_emb", "ix_example", "top_k", "top_p", "temperature", "repetition_penalty"],
            output_names=["y", "k", "v", "y_emb", "logits", "samples"],
            dynamic_axes={
                "iy": {1 : "iy_length"},
                "ik": {1 : "ik_length"},
                "iv": {1 : "iv_length"},
                "iy_emb": {1 : "iy_emb_length"},
                "ix_example": {1 : "ix_example_length"},
            },
            verbose=False,
            opset_version=16
        )


class VitsModel(nn.Module):
    def __init__(self, vits_path):
        super().__init__()
        dict_s2 = torch.load(vits_path,map_location="cpu")
        self.hps = dict_s2["config"]
        self.hps = DictToAttrRecursive(self.hps)
        self.hps.model.semantic_frame_rate = "25hz"
        self.vq_model = SynthesizerTrn(
            self.hps.data.filter_length // 2 + 1,
            self.hps.train.segment_size // self.hps.data.hop_length,
            n_speakers=self.hps.data.n_speakers,
            **self.hps.model
        )
        self.vq_model.eval()
        self.vq_model.load_state_dict(dict_s2["weight"], strict=False)
        
    def forward(self, text_seq, pred_semantic, ref_audio):
        refer = spectrogram_torch(
            ref_audio,
            self.hps.data.filter_length,
            self.hps.data.sampling_rate,
            self.hps.data.hop_length,
            self.hps.data.win_length,
            center=False
        )
        if debug_dump:
            print("refer", refer)
            print("phones2", text_seq)
        return self.vq_model(pred_semantic, text_seq, refer)[0, 0]


class GptSoVits(nn.Module):
    def __init__(self, vits, t2s):
        super().__init__()
        self.vits = vits
        self.t2s = t2s
    
    def forward(self, ref_seq, text_seq, ref_bert, text_bert, ref_audio, ssl_content):
        pred_semantic = self.t2s(ref_seq, text_seq, ref_bert, text_bert, ssl_content)
        if debug_dump:
            print("pred_semantic", pred_semantic)
        audio = self.vits(text_seq, pred_semantic, ref_audio)
        if debug_dump:
            print("audio", audio)
        if onnx_import:
            import onnxruntime
            sess = onnxruntime.InferenceSession("onnx/nahida/nahida_vits.onnx", providers=["CPU"])
            audio1 = sess.run(None, {
                "text_seq" : text_seq.detach().cpu().numpy(),
                "pred_semantic" : pred_semantic.detach().cpu().numpy(), 
                "ref_audio" : ref_audio.detach().cpu().numpy()
            })
            return audio, audio1
        return audio

    def export(self, ref_seq, text_seq, ref_bert, text_bert, ref_audio, ssl_content, project_name):
        self.t2s.export(ref_seq, text_seq, ref_bert, text_bert, ssl_content, project_name)
        pred_semantic = self.t2s(ref_seq, text_seq, ref_bert, text_bert, ssl_content)
        torch.onnx.export(
            self.vits,
            (text_seq, pred_semantic, ref_audio),
            f"onnx/{project_name}/{project_name}_vits.onnx",
            input_names=["text_seq", "pred_semantic", "ref_audio"],
            output_names=["audio"],
            dynamic_axes={
                "text_seq": {1 : "text_length"},
                "pred_semantic": {2 : "pred_length"},
                "ref_audio": {1 : "audio_length"},
            },
            opset_version=17,
            verbose=False
        )


class SSLModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.ssl = ssl_model

    def forward(self, ref_audio_16k):
        if onnx_import:
            import onnxruntime
            sess = onnxruntime.InferenceSession("onnx/nahida/nahida_cnhubert.onnx", providers=["CPU"])
            last_hidden_state = sess.run(None, {
                "ref_audio_16k" : ref_audio_16k.detach().cpu().numpy()
            })
            return torch.from_numpy(last_hidden_state[0])

        return self.ssl.model(ref_audio_16k)["last_hidden_state"].transpose(1, 2)

    def export(self, ref_audio_16k, project_name):
        self.ssl.model.eval()
        torch.onnx.export(
            self,
            (ref_audio_16k),
            f"onnx/{project_name}/{project_name}_cnhubert.onnx",
            input_names=["ref_audio_16k"],
            output_names=["last_hidden_state"],
            dynamic_axes={
                "ref_audio_16k": {1 : "text_length"},
                "last_hidden_state": {2 : "pred_length"}
            },
            opset_version=17,
            verbose=False
        )


def export(vits_path, gpt_path, project_name):
    vits = VitsModel(vits_path)
    gpt = T2SModel(gpt_path, vits)
    gpt_sovits = GptSoVits(vits, gpt)
    ssl = SSLModel()

    #ref_seq = torch.LongTensor([cleaned_text_to_sequence(["n", "i2", "h", "ao3", ",", "w", "o3", "sh", "i4", "b", "ai2", "y", "e4"])])
    #text_seq = torch.LongTensor([cleaned_text_to_sequence(["w", "o3", "sh", "i4", "b", "ai2", "y", "e4", "w", "o3", "sh", "i4", "b", "ai2", "y", "e4", "w", "o3", "sh", "i4", "b", "ai2", "y", "e4"])])

    ref_seq = torch.LongTensor([cleaned_text_to_sequence(['m', 'i', 'z', 'u', 'o', 'm', 'a', 'r', 'e', 'e', 'sh', 'i', 'a', 'k', 'a', 'r', 'a', 'k', 'a', 'w', 'a', 'n', 'a', 'k', 'U', 't', 'e', 'w', 'a', 'n', 'a', 'r', 'a', 'n', 'a', 'i', '.'])])
    text_seq = torch.LongTensor([cleaned_text_to_sequence(['y', 'u', 'z', 'u', 'k', 'i', 'y', 'u', 'k', 'a', 'r', 'i', 'g', 'a', 's', 'U', 'k', 'i', 'd', 'a', '!'])])
    
    #ref_seq = torch.LongTensor([cleaned_text_to_sequence(['a', 'a', 'r', 'u', 'b', 'u', 'i', 'sh', 'i', 'i', 'o', 'sh', 'i', 'y', 'o', 'o', 'sh', 'I', 't', 'a', 'b', 'o', 'i', 's', 'U', 'ch', 'e', 'N', 'j', 'a', 'a', 'o', 'ts', 'U', 'k', 'u', 'r', 'u', '.'])])
    #text_seq = torch.LongTensor([cleaned_text_to_sequence(['ky', 'o', 'o', 'w', 'a', 'h', 'a', 'r', 'e', 'd', 'e', 'sh', 'o', 'o', 'k', 'a', '?'])])
   
    ref_bert = torch.zeros((ref_seq.shape[1], 1024)).float()
    text_bert = torch.zeros((text_seq.shape[1], 1024)).float()
    ref_audio = torch.zeros((1, 48000 * 5)).float()

    ref_audio = torch.tensor([load_audio("JSUT.wav", 48000)]).float()
    ref_audio_16k = torchaudio.functional.resample(ref_audio,48000,16000).float()
    ref_audio_sr = torchaudio.functional.resample(ref_audio,48000,vits.hps.data.sampling_rate).float()

    import numpy as np
    import librosa
    zero_wav = np.zeros(
        int(vits.hps.data.sampling_rate * 0.3),
        dtype=np.float32,
    )
    wav16k, sr = librosa.load("JSUT.wav", sr=16000)
    wav16k = torch.from_numpy(wav16k)
    zero_wav_torch = torch.from_numpy(zero_wav)
    wav16k = torch.cat([wav16k, zero_wav_torch]).unsqueeze(0)
    ref_audio_16k = wav16k # hubertの入力のみpaddingする
    #ref_audio_sr = torchaudio.functional.resample(ref_audio_16k,16000,vits.hps.data.sampling_rate).float()

    try:
        os.mkdir(f"onnx/{project_name}")
    except:
        pass

    ssl_content = ssl(ref_audio_16k).float()
    if debug_dump:
        print("ssl_content", ssl_content)

    if onnx_import:
        a, b = gpt_sovits(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content)
        soundfile.write("out1.wav", a.cpu().detach().numpy(), vits.hps.data.sampling_rate)
        soundfile.write("out2.wav", b[0], vits.hps.data.sampling_rate)
        return

    if onnx_export:
        ssl.export(ref_audio_16k, project_name)

    a = gpt_sovits(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content).detach().cpu().numpy()

    soundfile.write("out.wav", a, vits.hps.data.sampling_rate)

    if onnx_export:
        gpt_sovits.export(ref_seq, text_seq, ref_bert, text_bert, ref_audio_sr, ssl_content, project_name)

        MoeVSConf = {
                "Folder" : f"{project_name}",
                "Name" : f"{project_name}",
                "Type" : "GPT-SoVits",
                "Rate" : vits.hps.data.sampling_rate,
                "NumLayers": gpt.t2s_model.num_layers,
                "EmbeddingDim": gpt.t2s_model.embedding_dim,
                "Dict": "BasicDict",
                "BertPath": "chinese-roberta-wwm-ext-large",
                "Symbol": symbols,
                "AddBlank": False
            }
        
        MoeVSConfJson = json.dumps(MoeVSConf)
        with open(f"onnx/{project_name}.json", 'w') as MoeVsConfFile:
            json.dump(MoeVSConf, MoeVsConfFile, indent = 4)


if __name__ == "__main__":
    try:
        os.mkdir("onnx")
    except:
        pass

    gpt_path = "GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt"#"GPT_weights/nahida-e25.ckpt"
    vits_path = "GPT_SoVITS/pretrained_models/s2G488k.pth"#"SoVITS_weights/nahida_e30_s3930.pth"
    exp_path = "nahida"
    export(vits_path, gpt_path, exp_path)

    # soundfile.write("out.wav", a, vits.hps.data.sampling_rate)