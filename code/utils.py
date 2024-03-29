import datetime
import os
import re 
import random 
import openai
import json
import logging
import time  
import jsonlines 
import requests 
import io
import pickle
import tempfile
from contextlib import contextmanager

# Load config 
config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.json')
with open(config_path, 'r') as f:
    config = json.load(f)



logger = logging.getLogger(__name__)

file_handler = logging.FileHandler('log.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)  # 设置日志级别
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO) 
# 设置日志格式
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s [%(filename)s:%(lineno)d]')
console_handler.setFormatter(formatter)

default_user = 'test_user'

import sys

class DualOutput:
    def __init__(self, filename):
        self.file = open(filename, 'a')
        self.stdout = sys.stdout

    def write(self, text):
        self.file.write(text)
        self.stdout.write(text)

    def flush(self):
        self.file.flush()
        self.stdout.flush()

    def close(self):
        self.file.close()



def json2string(_json):
    return json.dumps(_json)#, indent=4) #f"""```json\n{json.dumps(_json, indent=4)}\n```"""

# -----------------------------------------------------------------------------
# utilities for safe writing of a pickle file

# Context managers for atomic writes courtesy of
# http://stackoverflow.com/questions/2333872/atomic-writing-to-file-with-python
@contextmanager
def _tempfile(*args, **kws):
    """ Context for temporary file.
    Will find a free temporary filename upon entering
    and will try to delete the file on leaving
    Parameters
    ----------
    suffix : string
        optional file suffix
    """

    fd, name = tempfile.mkstemp(*args, **kws)
    os.close(fd)
    try:
        yield name
    finally:
        try:
            os.remove(name)
        except OSError as e:
            if e.errno == 2:
                pass
            else:
                raise e

def safe_pickle_dump(obj, fname):
    """
    prevents a case where one process could be writing a pickle file
    while another process is reading it, causing a crash. the solution
    is to write the pickle file to a temporary file and then move it.
    """
    with open_atomic(fname, 'wb') as f:
        pickle.dump(obj, f, -1) # -1 specifies highest binary protocol

@contextmanager
def open_atomic(filepath, *args, **kwargs):
    """ Open temporary file object that atomically moves to destination upon
    exiting.
    Allows reading and writing to and from the same filename.
    Parameters
    ----------
    filepath : string
        the file path to be opened
    fsync : bool
        whether to force write the file to disk
    kwargs : mixed
        Any valid keyword arguments for :code:`open`
    """
    fsync = kwargs.pop('fsync', False)

    original_permissions = os.stat(filepath).st_mode if os.path.exists(filepath) else None 

    with _tempfile(dir=os.path.dirname(filepath)) as tmppath:
        with open(tmppath, *args, **kwargs) as f:
            yield f
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.rename(tmppath, filepath)
        if original_permissions is not None:
            os.chmod(filepath, original_permissions)

def convert_to_timestamp(time_str: str):
    return time.mktime(datetime.datetime.strptime(time_str, "%Y-%m-%d").timetuple())

def _sync_file(path, obj=None):
    if obj:
        # save
        safe_pickle_dump(obj, path)
    else:
        # load
        if os.path.exists(path):
            with open(path, 'rb') as f:
                obj = pickle.load(f)
        else:
            obj = {}
    return obj 


def _sync_paper_collections(paper_collections=None):
    """Synchronize/Load paper collections with the database."""
    # 135
    # paper_collections_path = '/data/survey_agent/paper_collections.pkl'
    # 130
    paper_collections_path = f"{config['data_path']}/data/paper_collections.pkl"
    return _sync_file(paper_collections_path, paper_collections)

def _sync_chat_history(chat_history_dict=None):
    """Synchronize/Load chat history"""
    # 135
    # chat_history_path = '/data/survey_agent/chat_history.pkl'
    # 130
    chat_history_path = f"{config['data_path']}/data/chat_history.pkl"
    return _sync_file(chat_history_path, chat_history_dict)
    
cache_sign = True


#openai.proxy = config['proxy']
if config.get('proxy', None):
	openai.proxy = config['proxy']

if config.get('openai_apibase', None):
	openai.api_base = config['openai_apibase']

if config.get('gemini_apikey', None):
	import google.generativeai as genai
	genai.configure(api_key=config['gemini_apikey'])
	print('gemini key: {}'.format(config['gemini_apikey']))


cache = None 
def cached(func):
	def wrapper(*args, **kwargs):		
		global cache
		cache_path = 'cache_gemini.pkl'
		if cache == None:
			if not os.path.exists(cache_path):
				cache = {}
			else:
				cache = pickle.load(open(cache_path, 'rb'))  

		key = ( func.__name__, str(args), str(kwargs.items()))
		

		if (cache_sign and key in cache and cache[key] not in [None, '[TOKEN LIMIT]']) :
			return cache[key]
		else:
			result = func(*args, **kwargs)
			if result != 'busy' and result != None:
				cache[key] = result
				pickle.dump(cache, open(cache_path, 'wb'))
				#safe_pickle_dump(cache, cache_path)
			return result

	return wrapper

def get_response(sys_prompt, inputs, model='gemini', nth_generation=0):
	model = model.lower().replace(' ', '')
	if model.startswith('gpt-3.5'):
		model = 'gpt-3.5-turbo-1106'
		return get_response_gpt(sys_prompt, inputs, model, nth_generation=nth_generation)
	elif model.startswith('gpt-4'):
		model = 'gpt-4-1106-preview'
		return get_response_gpt(sys_prompt, inputs, model, nth_generation=nth_generation)
	elif model.startswith('gemini'):
		model = 'gemini-pro'
		return get_response_gemini(sys_prompt, inputs, model, nth_generation=nth_generation)

if config.get('openai_apikey', None):
    from openai import OpenAI
    client = OpenAI(
        # This is the default and can be omitted
        api_key=config['openai_apikey'],
    )

@cached 
def get_response_gpt(sys_prompt, inputs, model='gpt-4', retry_count=0, nth_generation=0):

	query = [ {'role': 'system', 'content': sys_prompt}]
	if len(inputs) > 0:
		query.append({'role': 'user', 'content': inputs})
	
	try:
		temperature = 0.2 if nth_generation else 0 
		logger.info('ChatGPT SysPrompt:  ' + sys_prompt[:100])
		logger.info('ChatGPT Input:  ' + inputs[:100])
		response = client.chat.completions.create(
			model= model ,  # 对话模型的名称
			messages=query,
			temperature=temperature,  # 值在[0,1]之间，越大表示回复越具有不确定性
			top_p=1,
			frequency_penalty=0.0,  # [-2,2]之间，该值越大则更倾向于产生不同的内容
			presence_penalty=0.0,  # [-2,2]之间，该值越大则更倾向于产生不同的内容,
			timeout=60
		)


		logger.info('GPT Output: ' + response.choices[0].message.content[:100])
		return response.choices[0].message.content

	except openai.BadRequestError as e:
		logger.exception(e)
		
		return '[TOKEN LIMIT]'

	except Exception as e:
		# unknown exception
		logger.exception(e)

		if retry_count < 2:
			time.sleep(5)
			logger.warn("[OPEN_AI] RateLimit exceed, 第{}次重试".format(retry_count+1))
			return get_response_gpt(sys_prompt, inputs, model, retry_count+1, nth_generation) 

		print(f'Fail to get response after {retry_count} retry')

@cached 
def get_response_gemini(sys_prompt, inputs, model='gemini-pro', retry_count=0, nth_generation=0):

	try:
		gemini_model = genai.GenerativeModel(model_name=model)

		logger.info('Gemini SysPrompt:  ' + sys_prompt[:100])
		logger.info('Gemini Input:  ' + inputs[:100])
			

		response = gemini_model.generate_content(sys_prompt + inputs, generation_config=genai.types.GenerationConfig(
			candidate_count=1, 
			temperature = 0.2 if nth_generation else 0,
		))
		
		response = response.text
		logger.info('Gemini Output: ' + response[:100])
		return response

	except Exception as e:
		# unknown exception
		logger.exception(e)

		if retry_count < 2:
			time.sleep(5)
			logger.warn("[GEMINI_AI] RateLimit exceed, 第{}次重试".format(retry_count+1))
			return get_response_gemini(sys_prompt, inputs, model, retry_count+1, nth_generation) 

		print(f'Fail to get response after {retry_count} retry')

def string2json(llm_response):
	llm_response = llm_response.strip("`")
	if llm_response.startswith('json'):
		llm_response = llm_response[4:]
	
	try:
		json_response = json.loads(llm_response)
	except:
		try:
			llm_response = llm_response[llm_response.find("{"):]
			json_response = json.loads(llm_response)
		except:
			return False
	
	return json_response


def get_response_json(post_processing_func=string2json, **kwargs):
	
	nth_generation = 0

	while (True):
		response = get_response(**kwargs, nth_generation=nth_generation)
		#print(f'{nth_generation} generation: {response[:100]}')
		
		json_response = post_processing_func(response)
		#print(f'parse results: {json_response}')

		if json_response:
			break 
		else:
			nth_generation += 1
			if nth_generation > 10:
				import pdb; pdb.set_trace()
			

	return json_response

if __name__ == '__main__':
	print(get_response_gpt('hi !', '1+1=?'))