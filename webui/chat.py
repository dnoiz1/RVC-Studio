import json
import os
from types import SimpleNamespace
from llama_cpp import Llama
from lib.model_utils import get_hash
from tts_cli import generate_speech, load_stt_models, transcribe_speech
from webui.downloader import BASE_MODELS_DIR
import sounddevice as sd

from webui.utils import gc_collect
from . import config

from vc_infer_pipeline import get_vc, vc_single

def init_model_params():
    return SimpleNamespace(
        fname=None,n_ctx=2048,n_gpu_layers=0
    )
def init_model_config():
    return SimpleNamespace(
        # template placeholder
        prompt_template = "",
        # how dialogs appear
        chat_template = "",
        # Default system instructions
        instruction = "",
        # maps role names
        mapper={
            "CHARACTER": "",
            "USER": ""
        }
    )
def init_llm_options():
    return SimpleNamespace(
        top_k = 42,
        repeat_penalty = 1.1,
        frequency_penalty = 0.,
        presence_penalty = 0.,
        tfs_z = 1.0,
        mirostat_mode = 1,
        mirostat_tau = 5.0,
        mirostat_eta = 0.1,
        suffix = None,
        max_tokens = 256,
        temperature = .8,
        top_p = .9,
    )
    
def init_model_data():
    return {
        "params": vars(init_model_params()),
        "config": vars(init_model_config()),
        "options": vars(init_llm_options())
    }

def init_assistant_template():
    return SimpleNamespace(
        background = "",
        personality = "",
        examples = [{"role": "", "content": ""}],
        greeting = "",
        name = ""
    )

def load_character_data(fname):
    with open(fname,"r") as f:
        loaded_state = json.load(f)
    return loaded_state

def load_model(model_file,n_ctx,n_gpu_layers,**kwargs):
    model = Llama(model_file,
                  n_ctx=n_ctx,
                  n_gpu_layers=n_gpu_layers,
                  verbose=False
                  )
    return model

def load_model_data(model_file):
    fname = os.path.join(BASE_MODELS_DIR,"LLM","config.json")
    key = get_hash(model_file)
    model_data = init_model_data()

    with open(fname,"r") as f:
        data = json.load(f) if os.path.isfile(fname) else model_data
        if key in data:
            model_data["params"].update(data[key]["params"])
            model_data["config"].update(data[key]["config"])
            model_data["options"].update(data[key]["options"])

    return model_data

# Define a Character class
class Character:
    # Initialize the character with a name and a voice
    def __init__(self, voice_file, model_file, memory = 100, user=os.getlogin()):
        self.voice_file = voice_file
        self.model_file = model_file
        self.voice_model = None
        self.sst_model = None
        self.loaded = False
        self.sample_rate = 16000 # same rate as hubert model
        self.memory = memory
        self.messages = []
        # self.message_thread = Thread(target=self.process_chat,name="message queue",daemon=True)
        self.user = user
        self.is_recording = False
        self.context = ""

        #load data
        self.character_data = load_character_data(voice_file)
        self.model_data = load_model_data(self.model_file)
        self.name = self.character_data["assistant_template"]["name"]

    def __del__(self):
        self.unload()

    def stop_listening(self): pass # overwritten by speak_and_listen

    def load(self,verbose=False):
        assert not self.loaded, "Model is already loaded"
        self.voice_model = get_vc(self.character_data["voice"],config=config,device=config.device)
        self.stt_model = load_stt_models() #speech recognition
        if len(self.messages)==0 and self.character_data["assistant_template"]["greeting"] and self.user:
            greeting_message = { #add greeting message
                "role": self.character_data["assistant_template"]["name"],
                "content": self.character_data["assistant_template"]["greeting"].format(
                    name=self.character_data["assistant_template"]["name"], user=self.user)}
            output_audio = self.text_to_speech(greeting_message["content"])
            if (output_audio):
                sd.play(*output_audio)
                greeting_message["audio"] = output_audio
            self.messages.append(greeting_message)
        self.LLM = Llama(self.model_file,
                  n_ctx=self.model_data["params"]["n_ctx"],
                  n_gpu_layers=self.model_data["params"]["n_gpu_layers"],
                  verbose=verbose
                  )
        self.loaded=True
        # self.message_thread.start()

    def unload(self):
        del self.LLM, self.voice_model, self.stt_model
        gc_collect()
        self.loaded=False
        self.is_recording = False
        self.stop_listening()
        # self.message_thread.join(1.)

    # Define a method to convert audio numpy data to text using speecht5_asr
    def audio_to_text(self, input_audio):
        # Send the audio data to speecht5_asr as a prompt
        prompt = transcribe_speech(input_audio,self.stt_model)
        # Return the response text
        return prompt

    # Define a method to generate text using llamacpp model
    def generate_text(self, input_text):
        assert self.loaded, "Please load the models first"

        model_config = self.model_data["config"]
        # Send the input text to llamacpp model as a prompt
        self.context = self.build_context(input_text)
        generator = self.LLM.create_completion(
            self.context,stream=True,stop=[
                "*","\n",
                model_config["mapper"]["USER"],
                model_config["mapper"]["CHARACTER"],
                self.character_data["assistant_template"]["name"]
                ],**self.model_data["options"])
        
        for completion_chunk in generator:
            response = completion_chunk['choices'][0]['text']
            yield response

    def build_context(self,prompt):
        model_config = self.model_data["config"]
        assistant_template = self.character_data["assistant_template"]
        chat_mapper = {
            self.user: model_config["mapper"]["USER"],
            assistant_template["name"]: model_config["mapper"]["CHARACTER"]
        }
        # Concatenate chat history and system template
        examples = [
            model_config["chat_template"].format(role=model_config["mapper"][ex["role"]],content=ex["content"])
                for ex in assistant_template["examples"] if ex["role"] and ex["content"]]+[
            model_config["chat_template"].format(role=chat_mapper[ex["role"]],content=ex["content"])
                for ex in self.messages[-self.memory:]]
            
        instruction = model_config["instruction"].format(name=assistant_template["name"],user=self.user)
        persona = f"{assistant_template['background']} {assistant_template['personality']}"
        context = "\n".join(examples)
        
        chat_history_with_template = model_config["prompt_template"].format(
            context=context,
            instruction=instruction,
            persona=persona,
            name=assistant_template["name"],
            user=self.user,
            prompt=prompt
            )

        return chat_history_with_template

    # Define a method to convert text to speech
    def text_to_speech(self, text):
        tts_audio = generate_speech(text,method=self.character_data["tts_method"], speaker=self.name, device=config.device)
        output_audio = vc_single(input_audio=tts_audio,**self.voice_model,**self.character_data["tts_options"])
        return output_audio

    # Define a method to run the STT and TTS in the background and be non-blocking
    def speak_and_listen(self, st=None):
        assert self.loaded, "Please load the models first"
        import speech_recognition as sr

        # Create a speech recognizer instance
        r = sr.Recognizer()
        r.energy_threshold = 420
        r.adjust_for_ambient_noise = True
        r.vosk_model = self.stt_model
        self.is_recording = True

        # Create a microphone instance
        m = sr.Microphone(sample_rate=self.sample_rate)
        
        # Define a callback function that will be called when speech is detected
        def callback(recognizer, audio):
            try:
                if not self.is_recording:
                    self.stop_listening()
                    return
                sd.wait() # wait for audio to stop playing
                input_data = recognizer.recognize_vosk(audio) #use vosk if google is broken
                # input_data = recognizer.recognize_google(audio)
                input_data = json.loads(input_data)
                prompt = input_data["text"] if "text" in input_data else None
                if prompt is not None and type(prompt) is str:
                    print(f"{self.name} heard: {prompt}")
                    # st.chat_message(self.user).write(prompt)
                    full_response = ""
                    # with st.chat_message(self.name):
                    #     message_placeholder = st.empty()
                    for response in self.generate_text(prompt):
                        full_response += response
                            # message_placeholder.markdown(full_response)
                    audio = self.text_to_speech(full_response)
                    if audio: sd.play(*audio)
                    self.messages.append({"role": self.user, "content": prompt}) #add user prompt to history
                    self.messages.append({
                        "role": self.name,
                        "content": full_response,
                        "audio": audio
                        })
                    print(f"{self.name} said: {full_response}")
                
            except sr.UnknownValueError:
                print("Google Speech Recognition could not understand audio")
            except sr.RequestError as e:
                print("Could not request results from Google Speech Recognition service; {0}".format(e))
            except Exception as e:
                print(e)
        # Start listening to the microphone in the background and call the callback function when speech is detected
        self.stop_listening = r.listen_in_background(m, callback)
        print("listening to mic...")