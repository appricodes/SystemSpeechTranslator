"""
System Speech Translator for NVDA
This global plugin captures system audio (WASAPI loopback), transcribes it using xAI's STT,
and translates it into the user's preferred language using either Google Translate or Grok.
It includes a background retroactive memory feature and a dedicated NVDA settings panel.

* Note on Google Translate: This utilizes the undocumented 'gtx' endpoint. Heavy usage
  may result in temporary rate-limiting by Google.
* Note on Security: Standard to the NVDA ecosystem, the user's API key is stored in plain text
  in the user's configuration file.
"""


import globalPluginHandler
import ui
import config
import gui
from gui import guiHelper
import wx
from gui.settingsDialogs import SettingsPanel
from scriptHandler import script
from logHandler import log  # Replaced print() with NVDA's native logger
import threading
import wave
import os
import tempfile
import sys
import tones
import time
from pathlib import Path
import array
import collections
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
import json

# We use a global variable to store the active instance of our GlobalPlugin.
# This is necessary because NVDA's GUI panels (like SettingsPanel) are instantiated
# separately from the plugin itself. This allows the GUI to notify the plugin when
# settings (like the retroactive memory duration) are changed and saved.
_addon_instance = None

# Define the schema for NVDA's native configuration system.
# This dictates the data types and default values for our add-on's settings,
# ensuring NVDA knows how to load, save, and validate them in nvda.ini.
confspec = {
	"apiKey": "string(default='')",
	"targetLanguage": "string(default='TTS Language')",
	"showPopup": "boolean(default=false)",
	"retroactiveSeconds": "string(default='off')",
	"translatorEngine": "string(default='Google (Free)')"
}

# Dynamically add the 'lib' folder inside the add-on directory to Python's sys.path.
# NVDA add-ons often need third-party packages (like pyaudiowpatch)
# that are not included in NVDA's bundled Python environment.
lib_dir = str(Path(__file__).parent / "lib")

if lib_dir not in sys.path:
	sys.path.insert(0, lib_dir)

# Attempt to import our bundled third-party libraries.
# If they fail to import (e.g., corrupted add-on installation), we catch the error
# and set them to None. The plugin will check for these before attempting to record or translate.
try:
	import pyaudiowpatch as pa
except ImportError:
	pa = None

# A predefined list of supported translation languages. "TTS Language" is a special
# dynamic option that attempts to match the language currently spoken by the user's screen reader.
LANGUAGES = ["TTS Language"] + sorted([
	"English", "Spanish", "French", "German", "Mandarin Chinese",
	"Arabic", "Hindi", "Russian", "Portuguese", "Japanese", "Korean", "Italian",
	"Dutch", "Turkish", "Polish", "Swedish", "Indonesian", "Vietnamese", "Thai",
	"Persian", "Hebrew", "Greek", "Czech", "Danish", "Finnish", "Hungarian",
	"Norwegian", "Romanian", "Slovak", "Ukrainian", "Malay", "Bengali", "Urdu",
	"Tamil", "Telugu", "Marathi", "Gujarati", "Kannada", "Malayalam", "Punjabi"
])

# A mapping of display language names to their standard ISO language codes.
# This serves a dual purpose: sending the correct code to the translation APIs,
# and matching the language code reported by NVDA's synthesizer back to a display name.
LANGUAGE_CODES = {
	"Arabic": "ar", "Bengali": "bn", "Czech": "cs", "Danish": "da", "Dutch": "nl",
	"English": "en", "Finnish": "fi", "French": "fr", "German": "de", "Greek": "el",
	"Gujarati": "gu", "Hebrew": "he", "Hindi": "hi", "Hungarian": "hu", "Indonesian": "id",
	"Italian": "it", "Japanese": "ja", "Kannada": "kn", "Korean": "ko", "Malay": "ms",
	"Malayalam": "ml", "Mandarin Chinese": "zh-CN", "Marathi": "mr", "Norwegian": "no",
	"Persian": "fa", "Polish": "pl", "Portuguese": "pt", "Punjabi": "pa", "Romanian": "ro",
	"Russian": "ru", "Slovak": "sk", "Spanish": "es", "Swedish": "sv", "Tamil": "ta",
	"Telugu": "te", "Thai": "th", "Turkish": "tr", "Ukrainian": "uk", "Urdu": "ur",
	"Vietnamese": "vi"
}


class TranslationResultDialog(wx.Dialog):
	"""
	A custom wxPython dialog used to display the final transcription and translation.
	It presents two read-only multiline text boxes so the user can review both
	what was heard and how it was translated.
	"""
	
	def __init__(self, parent, original_text, translated_text):
		super(TranslationResultDialog, self).__init__(
			parent, title="Translation Result", size=(600, 500),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
		)
		
		# BoxSizer arranges elements vertically (top to bottom).
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		
		# Label and read-only text control for the original transcribed audio.
		lblOrig = wx.StaticText(self, label="Original Text (Transcribed):")
		mainSizer.Add(lblOrig, 0, wx.ALL, 5)
		
		self.origTextCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
		self.origTextCtrl.SetValue(original_text)
		mainSizer.Add(self.origTextCtrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
		
		# Label and read-only text control for the translated output.
		lblTrans = wx.StaticText(self, label="Translated Text:")
		mainSizer.Add(lblTrans, 0, wx.ALL, 5)
		
		self.transTextCtrl = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
		self.transTextCtrl.SetValue(translated_text)
		mainSizer.Add(self.transTextCtrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
		
		# Standard button sizer for standard OS-compliant button layouts.
		# Using wx.ID_CANCEL implicitly binds the Escape key to trigger this button's event.
		btnSizer = wx.StdDialogButtonSizer()
		closeBtn = wx.Button(self, wx.ID_CANCEL, label="Close")
		closeBtn.Bind(wx.EVT_BUTTON, self.onClose)
		btnSizer.AddButton(closeBtn)
		btnSizer.Realize()
		
		mainSizer.Add(btnSizer, 0, wx.EXPAND | wx.ALL, 5)
		self.SetSizer(mainSizer)
		
		# For accessibility: explicitly set focus to the translated text box
		# so the screen reader automatically reads the translation upon opening the dialog.
		self.transTextCtrl.SetFocus()
	
	def onClose(self, evt):
		# Safely destroy the dialog window to free memory.
		self.Destroy()


class SystemSpeechTranslatorSettingsPanel(SettingsPanel):
	"""
	The configuration UI injected into NVDA's Preferences -> Settings menu.
	"""
	title = "System Speech Translator"
	
	def makeSettings(self, settingsSizer):
		"""
		Builds the graphical controls for the settings panel.
		"""
		sHelper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		
		# HARD PROFILE OVERRIDE: NVDA supports configuration profiles (e.g., different settings for specific apps).
		# We want our API key and language settings to be global regardless of active profiles.
		# By directly querying `profiles[0]`, we fetch from the absolute base configuration.
		try:
			addon_conf = config.conf.profiles[0].get("systemSpeechTranslator", {})
		except Exception:
			addon_conf = config.conf.get("systemSpeechTranslator", {})
		
		# Retrieve existing values to populate the form fields
		current_api_key = addon_conf.get("apiKey", "")
		current_language = addon_conf.get("targetLanguage", "TTS Language")
		current_engine = addon_conf.get("translatorEngine", "Google (Free)")
		
		# Handle the popup toggle. It might be stored as a string or a bool depending on config validation.
		raw_popup = addon_conf.get("showPopup", False)
		current_show_popup = raw_popup.lower() == "true" if isinstance(raw_popup, str) else bool(raw_popup)
		
		# The retroactive memory requires a string match for the Dropdown (Choice) control.
		current_retro_seconds = str(addon_conf.get("retroactiveSeconds", "off"))
		
		# API Key UI Configuration
		apiLabel = wx.StaticText(self, label="XAI API Key (Required for Audio Capture):")
		sHelper.addItem(apiLabel)
		
		# Create a horizontal row containing the password field and the help button
		apiSizer = wx.BoxSizer(wx.HORIZONTAL)
		
		# We use two text controls for the API key: one masked (password) and one visible.
		# They are swapped dynamically based on the "Show API Key" checkbox to protect sensitive data.
		self.apiKeyCtrl_hidden = wx.TextCtrl(self, value=current_api_key, style=wx.TE_PASSWORD)
		apiSizer.Add(self.apiKeyCtrl_hidden, 1, wx.EXPAND | wx.RIGHT, 5)
		
		self.apiKeyCtrl_visible = wx.TextCtrl(self, value=current_api_key, style=wx.TE_MULTILINE | wx.TE_DONTWRAP,
											  size=(-1, 60))
		self.apiKeyCtrl_visible.Hide()
		apiSizer.Add(self.apiKeyCtrl_visible, 1, wx.EXPAND | wx.RIGHT, 5)
		
		getApiKeyBtn = wx.Button(self, label="How to get API Key")
		getApiKeyBtn.Bind(wx.EVT_BUTTON, self.onGetApiKey)
		apiSizer.Add(getApiKeyBtn, 0, wx.ALIGN_CENTER_VERTICAL)
		
		sHelper.addItem(apiSizer)
		
		# Checkbox to toggle the API Key mask
		self.showApiCheck = wx.CheckBox(self, label="Show API Key")
		self.showApiCheck.Bind(wx.EVT_CHECKBOX, self.onToggleApiVisibility)
		sHelper.addItem(self.showApiCheck)
		
		# Dropdown for selecting the underlying translation service
		ENGINE_OPTIONS = ["Google (Free)", "Grok (Premium)"]
		self.engineChoice = sHelper.addLabeledControl(
			"Translation Engine:", wx.Choice, choices=ENGINE_OPTIONS
		)
		if current_engine in ENGINE_OPTIONS:
			self.engineChoice.SetStringSelection(current_engine)
		else:
			self.engineChoice.SetSelection(0)
		
		# Dropdown for target translation language
		self.languageChoice = sHelper.addLabeledControl(
			"Target Language:", wx.Choice, choices=LANGUAGES
		)
		if current_language in LANGUAGES:
			self.languageChoice.SetStringSelection(current_language)
		else:
			self.languageChoice.SetSelection(0)
		
		# Checkbox determining whether output goes directly to speech or opens the visual dialog
		self.showPopupCheckBox = sHelper.addItem(
			wx.CheckBox(self, label="Show results in a popup window")
		)
		self.showPopupCheckBox.SetValue(current_show_popup)
		
		# Dropdown to control how many seconds of audio are continuously recorded in the background.
		# "off" completely disables the background thread's memory buffering.
		RETRO_OPTIONS = ["off", "5", "10", "15", "20", "25", "30", "40", "50", "60", "70", "80", "90", "100", "110",
						 "120"]
		self.retroSecondsChoice = sHelper.addLabeledControl(
			"Retroactive memory duration (seconds):", wx.Choice, choices=RETRO_OPTIONS
		)
		if current_retro_seconds in RETRO_OPTIONS:
			self.retroSecondsChoice.SetStringSelection(current_retro_seconds)
		else:
			self.retroSecondsChoice.SetStringSelection("off")
	
	def onGetApiKey(self, event):
		"""
		Handles the 'How to get API Key' button press.
		Calculates the physical file path of the add-on on the user's hard drive
		to locate and open the localized HTML instruction manual.
		"""
		import languageHandler
		
		# Traverse up the directory tree to find the root add-on folder.
		# Current path: addon_root/globalPlugins/SystemSpeechTranslator/__init__.py
		plugin_dir = os.path.dirname(os.path.abspath(__file__))
		global_plugins_dir = os.path.dirname(plugin_dir)
		addon_dir = os.path.dirname(global_plugins_dir)
		
		# Get NVDA's current language to attempt serving a translated help document.
		lang = languageHandler.getLanguage()
		
		# Fallback sequence: Full Locale -> Base Language -> English -> Root Doc
		paths_to_check = [
			os.path.join(addon_dir, "doc", lang, "grok_api_instructions.html"),
			os.path.join(addon_dir, "doc", lang.split("_")[0], "grok_api_instructions.html") if "_" in lang else None,
			os.path.join(addon_dir, "doc", "en", "grok_api_instructions.html"),
			os.path.join(addon_dir, "doc", "grok_api_instructions.html")
		]
		
		doc_path = None
		for path in paths_to_check:
			if path and os.path.exists(path):
				doc_path = path
				break
		
		if doc_path:
			webbrowser.open("file://" + doc_path)
		else:
			ui.message("API instructions document not found. Please ensure it is placed in the doc folder.")
	
	def onToggleApiVisibility(self, event):
		"""
		Syncs text between the hidden and visible text controls, then toggles
		which one is currently drawn on the screen based on the checkbox state.
		"""
		if self.showApiCheck.IsChecked():
			self.apiKeyCtrl_visible.SetValue(self.apiKeyCtrl_hidden.GetValue())
			self.apiKeyCtrl_hidden.Hide()
			self.apiKeyCtrl_visible.Show()
		else:
			self.apiKeyCtrl_hidden.SetValue(self.apiKeyCtrl_visible.GetValue())
			self.apiKeyCtrl_visible.Hide()
			self.apiKeyCtrl_hidden.Show()
		
		# Force wxWidgets to recalculate layout geometry since child visibility changed.
		self.Layout()
	
	def onSave(self):
		"""
		Triggered when the user clicks 'Apply' or 'OK' in the NVDA settings dialog.
		"""
		global _addon_instance
		
		# Restore the temporary profile name hijack so NVDA's config system finishes saving cleanly.
		if getattr(self, "originalProfileName", None) is not None:
			try:
				config.conf.profiles[-1].name = self.originalProfileName
			except Exception:
				pass
		
		# Extract the correct API key string based on which control is currently populated/visible.
		if self.showApiCheck.IsChecked():
			val = self.apiKeyCtrl_visible.GetValue()
		else:
			val = self.apiKeyCtrl_hidden.GetValue()
		
		# Write strictly to the base profile to ensure these settings are global across all NVDA profiles.
		try:
			target_conf = config.conf.profiles[0]
		except Exception:
			target_conf = config.conf
		
		if "systemSpeechTranslator" not in target_conf:
			target_conf["systemSpeechTranslator"] = {}
		
		# Commit the UI values to NVDA's config dictionary in memory.
		target_conf["systemSpeechTranslator"]["apiKey"] = val.strip()
		target_conf["systemSpeechTranslator"]["translatorEngine"] = self.engineChoice.GetStringSelection()
		target_conf["systemSpeechTranslator"]["targetLanguage"] = self.languageChoice.GetStringSelection()
		target_conf["systemSpeechTranslator"]["showPopup"] = self.showPopupCheckBox.GetValue()
		target_conf["systemSpeechTranslator"]["retroactiveSeconds"] = self.retroSecondsChoice.GetStringSelection()
		
		# If the plugin is actively running, alert it that settings have changed so it can
		# dynamically resize the retroactive audio ring buffer without restarting NVDA.
		if _addon_instance and hasattr(_addon_instance, "_apply_retro_config"):
			_addon_instance._apply_retro_config()
	
	def onDiscard(self):
		"""
		Triggered if the user hits 'Cancel'. We simply restore the profile name state.
		"""
		if getattr(self, "originalProfileName", None) is not None:
			try:
				config.conf.profiles[-1].name = self.originalProfileName
			except Exception:
				pass
	
	def onPanelActivated(self):
		"""
		Called when the user navigates to this panel in the settings dialog.
		We temporarily erase the active profile name. This prevents NVDA from warning the
		user that they are editing a profile-specific setting, because our settings are enforced globally.
		"""
		try:
			self.originalProfileName = config.conf.profiles[-1].name
			config.conf.profiles[-1].name = None
		except Exception:
			self.originalProfileName = None
		self.Show()
	
	def onPanelDeactivated(self):
		"""
		Called when the user navigates to a different panel. Restores the profile name.
		"""
		if getattr(self, "originalProfileName", None) is not None:
			try:
				config.conf.profiles[-1].name = self.originalProfileName
			except Exception:
				pass
		self.Hide()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""
	The core logical class of the add-on. Instantiated once by NVDA upon startup.
	Handles keystrokes, background recording threads, and API communications.
	"""
	
	def __init__(self):
		global _addon_instance
		super(GlobalPlugin, self).__init__()
		_addon_instance = self
		
		# State tracking for manual recording triggers
		self.is_recording = False
		self._manual_audio_data = []  # Buffer specifically for manual recording
		self._retro_click_timer = None
		self._active_translation_dialog = None
		
		# Synchronization lock to safely append/read/resize the background audio buffer
		# across the main NVDA thread and the background audio capturing thread.
		self._retro_lock = threading.Lock()
		
		# The ring buffer. Uses a Python deque with a maxlen. If maxlen is hit,
		# adding a new audio chunk automatically drops the oldest chunk, maintaining a rolling window.
		self.audio_ring_buffer = collections.deque(maxlen=1)
		
		# Threading control flags
		self._run_retro_thread = False
		self._reinit_audio = False  # Allows us to gracefully reboot the audio device stream on the fly.
		self._retro_thread_obj = None
		self._retro_stream = None
		
		# Hardware tracking variables
		self._retro_sample_rate = 48000
		self._retro_channels = 2
		self._buffered_channels = 1
		self._buffered_sample_rate = 16000
		
		# Clean up any leftover files from the last time NVDA was running
		self._cleanup_temp_files()
		
		# Tell NVDA's configuration manager that 'systemSpeechTranslator' is a base-only section.
		# This stops NVDA from writing these settings into application-specific profile files.
		section_name = "systemSpeechTranslator"
		for obj in (getattr(config, "ConfigManager", None), getattr(config, "conf", None)):
			if obj and hasattr(obj, "BASE_ONLY_SECTIONS"):
				try:
					if isinstance(obj.BASE_ONLY_SECTIONS, set):
						obj.BASE_ONLY_SECTIONS.add(section_name)
					else:
						obj.BASE_ONLY_SECTIONS = frozenset(obj.BASE_ONLY_SECTIONS | {section_name})
				except Exception:
					pass
		
		# Inject our schema specification into NVDA's config engine.
		config.conf.spec["systemSpeechTranslator"] = confspec
		
		# Hard-bind self.addonConf strictly to the BASE profile.
		# This guarantees that at runtime, the plugin reads global settings, not profile-overridden ones.
		try:
			base_conf = config.conf.profiles[0]
			if "systemSpeechTranslator" not in base_conf:
				base_conf["systemSpeechTranslator"] = {}
			self.addonConf = base_conf["systemSpeechTranslator"]
		except Exception:
			# Safety fallback in case profiles[0] logic changes in future NVDA versions.
			if "systemSpeechTranslator" not in config.conf:
				config.conf["systemSpeechTranslator"] = {}
			self.addonConf = config.conf["systemSpeechTranslator"]
		
		# Register the Settings Panel class so NVDA knows to add it to the GUI menu.
		gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(SystemSpeechTranslatorSettingsPanel)
		
		# Initialize the background recording buffer based on user settings.
		self._apply_retro_config()
	
	def terminate(self):
		"""
		Lifecycle hook called by NVDA when the add-on is disabled, reloaded, or NVDA exits.
		Handles safe cleanup of threads and GUI injections.
		"""
		global _addon_instance
		self._run_retro_thread = False
		self._reinit_audio = True  # Instantly breaks the inner audio loop to allow the thread to die.
		
		# Clean up current session files before closing
		self._cleanup_temp_files()
		
		try:
			gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(SystemSpeechTranslatorSettingsPanel)
		except ValueError:
			pass
		_addon_instance = None
		super(GlobalPlugin, self).terminate()
	
	def _cleanup_temp_files(self):
		"""
		Sweeps the system temp directory for orphaned audio files from previous crashed
		sessions or failed deletions, keeping the user's hard drive clean.
		"""
		temp_dir = tempfile.gettempdir()
		for filename in os.listdir(temp_dir):
			if filename.startswith("nvda_sst_") and filename.endswith(".wav"):
				try:
					os.remove(os.path.join(temp_dir, filename))
				except Exception:
					pass  # If it's currently locked by antivirus or another process, skip it.
	
	def _apply_retro_config(self):
		"""
		Dynamically recalculates and resizes the retroactive audio ring buffer
		when settings change, and manages the background recording thread lifecycle.
		"""
		retro_setting = self.addonConf.get("retroactiveSeconds", "off")
		
		if retro_setting == "off":
			seconds = 0
		else:
			try:
				seconds = int(retro_setting)
			except ValueError:
				seconds = 0
		
		# If the duration changed, safely copy existing audio into a new buffer
		# with the newly defined length limit.
		with self._retro_lock:
			if self.audio_ring_buffer.maxlen != seconds:
				new_buffer = collections.deque(maxlen=seconds)
				new_buffer.extend(self.audio_ring_buffer)
				self.audio_ring_buffer = new_buffer
		
		# We enforce a constant background thread. If the feature is "off",
		# the thread still runs but immediately discards audio. This prevents crashes
		# related to repeatedly spawning/killing PyAudio instances in a Windows environment.
		self._run_retro_thread = True
		if self._retro_thread_obj is None or not self._retro_thread_obj.is_alive():
			self._reinit_audio = False
			self._retro_thread_obj = threading.Thread(target=self._retro_recorder_thread, daemon=True)
			self._retro_thread_obj.start()
		else:
			# If the thread is already running, toggle _reinit_audio to force it
			# to restart its stream connection seamlessly.
			self._reinit_audio = True
	
	def _retro_recorder_thread(self):
		"""
		The unified daemon thread responsible for capturing WASAPI loopback audio.
		It concurrently services BOTH the background retroactive memory and the active manual recording.
		"""
		CHUNK = 1024
		while self._run_retro_thread:
			# Guard against missing dependencies before executing.
			if not pa:
				time.sleep(2)
				continue
			
			p = pa.PyAudio()
			try:
				# Query Windows via WASAPI to find the default speaker device.
				wasapi_info = p.get_host_api_info_by_type(pa.paWASAPI)
				default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
				
				# We specifically need the "loopback" version of the speaker device.
				# If the default isn't marked as loopback, we scan available devices
				# to find the hidden loopback clone by string matching the name.
				if not default_speakers["isLoopbackDevice"]:
					for loopback in p.get_loopback_device_info_generator():
						if default_speakers["name"] in loopback["name"]:
							default_speakers = loopback
							break
				
				self._retro_sample_rate = int(default_speakers["defaultSampleRate"])
				self._retro_channels = default_speakers["maxInputChannels"]
				
				# Open an input stream directly from the system speakers.
				self._retro_stream = p.open(format=pa.paInt16,
											channels=self._retro_channels,
											rate=self._retro_sample_rate,
											input=True,
											frames_per_buffer=CHUNK,
											input_device_index=default_speakers["index"])
				
				raw_accumulator = []
				frames_accumulated = 0
				# Determine how many raw frames equate to exactly 1 second of audio.
				frames_per_block = self._retro_sample_rate
				
				# Hard limit bound calculation (120 seconds max manual recording context)
				max_manual_chunks = int((self._retro_sample_rate / CHUNK) * 120)
				
				while self._run_retro_thread:
					# If the main thread requests a re-init (e.g., audio device changed, settings saved),
					# break out of this inner loop. The outer loop will immediately restart PyAudio.
					if self._reinit_audio:
						self._reinit_audio = False
						break
					
					# Read a small chunk of audio without blocking if overflow occurs.
					data = self._retro_stream.read(CHUNK, exception_on_overflow=False)
					
					# Unified capture logic: Funnel data to the manual accumulator if active
					if self.is_recording:
						self._manual_audio_data.append(data)
						
						# Protect against infinitely looping recording / memory exhaustion
						if len(self._manual_audio_data) >= max_manual_chunks:
							self.is_recording = False
							wx.CallAfter(tones.beep, 400, 100)
							wx.CallAfter(ui.message, "processing")
							self._trigger_manual_processing()
					
					with self._retro_lock:
						is_active = self.audio_ring_buffer.maxlen > 0
					
					if is_active:
						# Accumulate small chunks until we have exactly 1 second of audio.
						raw_accumulator.append(data)
						frames_accumulated += CHUNK
						
						if frames_accumulated >= frames_per_block:
							block_bytes = b''.join(raw_accumulator)
							
							# Optimize memory and API upload time by heavily downsampling the raw audio.
							downsampled_bytes, f_channels, f_rate = self._downsample_audio(
								block_bytes, self._retro_channels, self._retro_sample_rate
							)
							
							self._buffered_channels = f_channels
							self._buffered_sample_rate = f_rate
							
							# Push the finalized 1-second block into the deque.
							# If the deque is full, the oldest block is automatically dropped.
							with self._retro_lock:
								self.audio_ring_buffer.append(downsampled_bytes)
							
							raw_accumulator = []
							frames_accumulated = 0
					else:
						# If feature is set to 'off', we MUST still consume the stream data.
						# If we don't `read()` from WASAPI, the Windows audio buffer overflows
						# and causes driver-level glitches or crashes. We read it, but throw it away.
						if frames_accumulated > 0:
							raw_accumulator.clear()
							frames_accumulated = 0
			
			except Exception as e:
				# If device is lost or errors out, wait a moment and try reopening.
				log.error(f"System Speech Translator - Background Recording Loop Error: {e}")
				time.sleep(2)
			finally:
				# Safe teardown of PyAudio instances upon loop break.
				if self._retro_stream:
					try:
						self._retro_stream.stop_stream()
						self._retro_stream.close()
					except Exception:
						pass
					self._retro_stream = None
				p.terminate()
	
	def _downsample_audio(self, raw_bytes, native_channels, native_sample_rate):
		"""
		Reduces audio fidelity (Stereo -> Mono, 48kHz -> 16kHz) to drastically shrink file sizes.
		xAI's STT model expects and performs perfectly fine on 16kHz mono audio.
		"""
		# Convert raw byte stream to a mutable array of 16-bit integers
		samples = array.array('h', raw_bytes)
		
		# If stereo, average Left and Right channels to safely convert to Mono
		# without losing audio panned exclusively to one side.
		if native_channels == 2:
			left = samples[0::2]
			right = samples[1::2]
			samples = array.array('h', ((l + r) // 2 for l, r in zip(left, right)))
			final_channels = 1
		else:
			final_channels = native_channels
		
		# Calculate ratio to step through the sample rate array.
		# e.g., 48000 / 16000 = 3, so we take every 3rd data point.
		downsample_factor = native_sample_rate // 16000
		if downsample_factor > 1:
			samples = samples[::downsample_factor]
			final_sample_rate = native_sample_rate // downsample_factor
		else:
			final_sample_rate = native_sample_rate
		
		return samples.tobytes(), final_channels, final_sample_rate
	
	@script(
		description="Handles multi-press translator actions: Single-press maps retro, Double-press starts manual recording.",
		category="System Speech Translator",
		gesture="kb:NVDA+shift+escape"
	)
	def script_handleTranslatorEsc(self, gesture):
		"""
		Primary hotkey multiplexer. Differentiates between a single rapid press
		and a rapid double-press to assign two different commands to one hotkey.
		"""
		if not pa:
			ui.message("Required library (PyAudioWPatch) not found.")
			return
		
		# If a recording is actively occurring, ANY press of this hotkey will
		# instantly abort the recording phase and move to the translation phase.
		if self.is_recording:
			if self._retro_click_timer and self._retro_click_timer.IsRunning():
				self._retro_click_timer.Stop()
			self.is_recording = False
			tones.beep(400, 100)
			ui.message("processing")
			self._trigger_manual_processing()
			return
		
		if self._retro_click_timer and self._retro_click_timer.IsRunning():
			# If the timer is still ticking from the FIRST press, and the user hits it again,
			# we interpret this as a DOUBLE PRESS. Cancel the timer and start manual recording.
			self._retro_click_timer.Stop()
			
			api_key = self.addonConf.get("apiKey", "")
			if not api_key:
				ui.message("Please enter your XAI API key in the NVDA settings.")
				return
			
			threading.Thread(target=self._delayed_record_start).start()
		else:
			# FIRST PRESS: Set a short 350ms delay. If no second press happens before
			# this timer runs out, it executes the single-press logic (_execute_retro_translation).
			self._retro_click_timer = wx.CallLater(350, self._execute_retro_translation)
	
	@script(
		description="Toggle manual system audio recording for translation",
		category="System Speech Translator",
		gesture="kb:NVDA+shift+r"
	)
	def script_toggleRecord(self, gesture):
		"""
		Secondary dedicated hotkey for purely toggling the manual recording mode.
		"""
		if not pa:
			ui.message("Required library (PyAudioWPatch) not found.")
			return
		
		api_key = self.addonConf.get("apiKey", "")
		if not api_key:
			ui.message("Please enter your XAI API key in the NVDA settings.")
			return
		
		if not self.is_recording:
			threading.Thread(target=self._delayed_record_start).start()
		else:
			self.is_recording = False
			tones.beep(400, 100)
			ui.message("processing")
			self._trigger_manual_processing()
	
	def _delayed_record_start(self):
		"""
		A helper that announces "Recording", pauses momentarily so NVDA finishes speaking,
		emits a beep, and then initiates the audio stream logic instantly.
		"""
		ui.message("Recording")
		time.sleep(0.7)
		tones.beep(800, 100)
		
		# Reset the manual buffer and flip the flag. The background thread
		# will instantly begin accumulating frames into this buffer on its next loop.
		self._manual_audio_data = []
		self.is_recording = True
	
	def _execute_retro_translation(self):
		"""
		Extracts the current state of the retroactive audio buffer and sends it for processing.
		Called by the single-press action of the NVDA+Shift+Escape hotkey.
		"""
		retro_setting = self.addonConf.get("retroactiveSeconds", "off")
		if retro_setting == "off":
			ui.message("Retroactive recording is disabled in settings.")
			return
		
		api_key = self.addonConf.get("apiKey", "")
		if not api_key:
			ui.message("Please enter your XAI API key in the NVDA settings.")
			return
		
		tones.beep(800, 100)
		ui.message("Translating recent audio...")
		
		# Safely copy the list elements out of the deque before the background thread modifies it.
		with self._retro_lock:
			chunks_copy = list(self.audio_ring_buffer)
		
		if not chunks_copy:
			ui.message("No audio in memory yet.")
			return
		
		# Offload the disk I/O and network requests to a background thread to prevent freezing NVDA.
		threading.Thread(
			target=self._process_retroactive_audio,
			args=(chunks_copy,),
			daemon=True
		).start()
	
	def _process_retroactive_audio(self, chunks):
		"""
		Flushes the memory buffer to a temporary WAV file on disk, processes it,
		and signals the background audio thread to resume capturing.
		"""
		final_bytes = b''.join(chunks)
		
		# Generate a cryptographically secure, random temporary file path
		fd, wav_path = tempfile.mkstemp(prefix="nvda_sst_retro_", suffix=".wav")
		os.close(fd)  # Close OS-level handle so wave module can open it
		
		# Construct standard WAV headers based on the downsampled parameters.
		with wave.open(wav_path, 'wb') as wf:
			wf.setnchannels(self._buffered_channels)
			wf.setsampwidth(2)
			wf.setframerate(self._buffered_sample_rate)
			wf.writeframes(final_bytes)
		
		self._process_file(wav_path)
		
		# Restart the background capture loop now that we are done utilizing the audio lock.
		self._reinit_audio = True
	
	def _trigger_manual_processing(self):
		"""
		Safely isolates the manual audio buffer generated by the background thread,
		clears the array, and spawns the payload processing thread.
		"""
		audio_copy = self._manual_audio_data[:]
		self._manual_audio_data = []
		
		if not audio_copy:
			wx.CallAfter(ui.message, "No audio captured.")
			return
		
		raw_bytes = b''.join(audio_copy)
		
		threading.Thread(
			target=self._process_manual_audio,
			args=(raw_bytes, self._retro_channels, self._retro_sample_rate),
			daemon=True
		).start()
	
	def _process_manual_audio(self, raw_bytes, native_channels, native_sample_rate):
		"""
		Downsamples and encodes the unified manual audio byte string into a valid WAV payload.
		"""
		try:
			# Downsample before writing to disk for API efficiency.
			final_bytes, final_channels, final_sample_rate = self._downsample_audio(
				raw_bytes, native_channels, native_sample_rate
			)
			
			# Generate a cryptographically secure temporary file for the recording
			fd, wav_path = tempfile.mkstemp(prefix="nvda_sst_rec_", suffix=".wav")
			os.close(fd)
			
			with wave.open(wav_path, 'wb') as wf:
				wf.setnchannels(final_channels)
				wf.setsampwidth(2)
				wf.setframerate(final_sample_rate)
				wf.writeframes(final_bytes)
			
			self._process_file(wav_path)
		
		except Exception as e:
			log.error(f"System Speech Translator - Process Manual Audio Error: {e}")
	
	def _process_file(self, wav_path):
		"""
		The orchestrator function linking STT -> Translation -> Output.
		Handles calling the respective API functions and managing the final payload cleanup.
		"""
		raw_popup = self.addonConf.get("showPopup", False)
		show_popup = raw_popup.lower() == "true" if isinstance(raw_popup, str) else bool(raw_popup)
		
		try:
			# 1. Transcribe the audio file.
			transcribed_text = self._stt(wav_path)
			if transcribed_text is None:
				return
			
			transcribed_text = transcribed_text.strip()
			if not transcribed_text:
				ui.message("No speech recognized.")
				return
			
			# 2. Pass the transcription to the translation engine.
			translated_text = self._translate(transcribed_text)
			if translated_text:
				translated_text = translated_text.strip()
				
				# 3. Deliver results to the user via their preferred method (GUI vs pure speech).
				if show_popup:
					# GUI updates MUST happen on the main thread. wx.CallAfter pushes this to the main loop.
					wx.CallAfter(self._show_translation_dialog, transcribed_text, translated_text)
				else:
					ui.message(translated_text)
		
		finally:
			# Ensure the temporary audio file is deleted.
			# We use a short retry loop because Windows Antivirus often locks newly
			# created files for a fraction of a second, which causes os.remove to fail.
			if os.path.exists(wav_path):
				for attempt in range(3):
					try:
						os.remove(wav_path)
						break  # Success, break out of the loop
					except Exception as e:
						if attempt == 2:  # On the final attempt, log the failure
							log.error(f"System Speech Translator - Failed to remove temporary audio file: {e}")
						else:
							time.sleep(0.5)  # Wait half a second and try again
	
	def _show_translation_dialog(self, original_text, translated_text):
		"""
		Constructs and launches the wxWidgets results window safely on the main UI thread.
		"""
		# Prevent stacking multiple dialogs. If one is open, forcefully close it first.
		if getattr(self, "_active_translation_dialog", None):
			try:
				self._active_translation_dialog.EndModal(wx.ID_CANCEL)
			except Exception:
				pass
			self._active_translation_dialog = None
		
		# NVDA standard practice: Wrap modal dialogs in prePopup/postPopup to temporarily
		# suspend normal keyboard interception so standard GUI keystrokes work properly.
		gui.mainFrame.prePopup()
		try:
			dlg = TranslationResultDialog(gui.mainFrame, original_text, translated_text)
			self._active_translation_dialog = dlg
			dlg.ShowModal()
		finally:
			self._active_translation_dialog = None
			try:
				dlg.Destroy()
			except Exception:
				pass
			gui.mainFrame.postPopup()
	
	def _get_target_language_info(self):
		"""
		Resolves the target language string.
		If set to "TTS Language", it programmatically asks NVDA's active synthesizer
		what its current language is, then cross-references the internal code table
		to extract a human-readable name for the translation prompts.
		"""
		target_lang = self.addonConf.get("targetLanguage", "TTS Language")
		if target_lang == "TTS Language":
			try:
				import synthDriverHandler
				synth_lang = synthDriverHandler.getSynth().language
				if not synth_lang:
					return "English"
				
				# Synthesizer codes might be "en_US", "en-us", etc. Normalize to snake_case.
				normalized_synth = synth_lang.replace("-", "_").lower()
				
				# Attempt exact locale match first (e.g., 'zh-CN')
				for name, code in LANGUAGE_CODES.items():
					if code.replace("-", "_").lower() == normalized_synth:
						return name
				
				# If exact locale fails, attempt a loose match on the primary language code (e.g., 'es' for 'es_MX')
				base_code = normalized_synth.split("_")[0]
				for name, code in LANGUAGE_CODES.items():
					if code.split("-")[0].lower() == base_code:
						return name
				
				# Fallback if the synthesizer uses a completely unknown code
				return "English"
			except Exception as e:
				log.error(f"System Speech Translator - Language Detection Error: {e}")
				return "English"
		return target_lang
	
	def _stt(self, file_path):
		"""
		Uploads the WAV file to xAI's STT (Speech-to-Text) API.
		Requests diarization (speaker separation) so that multi-person dialogue is properly attributed.
		Uses Python's built-in urllib to remain fully GPLv2 compliant.
		"""
		api_key = self.addonConf.get("apiKey", "")
		url = "https://api.x.ai/v1/stt"
		
		# Define a boundary string for the multipart/form-data payload
		boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
		
		try:
			with open(file_path, 'rb') as f:
				audio_bytes = f.read()
		except Exception as e:
			log.error(f"System Speech Translator - Failed to read audio file: {e}")
			return None
		
		# Construct the multipart/form-data body manually to avoid needing external libraries like 'requests'
		body = (
			f"--{boundary}\r\n"
			f"Content-Disposition: form-data; name=\"model\"\r\n\r\n"
			f"grok-stt\r\n"
			f"--{boundary}\r\n"
			f"Content-Disposition: form-data; name=\"diarize\"\r\n\r\n"
			f"true\r\n"
			f"--{boundary}\r\n"
			f"Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
			f"Content-Type: audio/wav\r\n\r\n"
		).encode('utf-8')
		
		footer = f"\r\n--{boundary}--\r\n".encode('utf-8')
		full_payload = body + audio_bytes + footer
		
		headers = {
			"Authorization": f"Bearer {api_key}",
			"Content-Type": f"multipart/form-data; boundary={boundary}"
		}
		
		req = urllib.request.Request(url, data=full_payload, headers=headers, method='POST')
		
		try:
			# Upload with a long timeout due to potentially large 120s audio files
			with urllib.request.urlopen(req, timeout=120) as response:
				if response.getcode() == 200:
					json_data = json.loads(response.read().decode('utf-8'))
					words = json_data.get('words', [])
					
					# If the model fails to diarize (e.g. poor audio quality), fallback to the raw text block.
					if not words:
						return json_data.get('text', '')
					
					# Reconstruct the sentence structure based on speaker ID changes.
					current_speaker = None
					dialogue_blocks = []
					current_sentence = []
					
					for item in words:
						word = item.get('text', '').strip()
						speaker_id = item.get('speaker', 0)
						
						# Force 1-based indexing for cleaner UI presentation (Speaker 1, Speaker 2...)
						display_speaker = speaker_id + 1 if isinstance(speaker_id, int) else speaker_id
						
						if display_speaker != current_speaker:
							if current_speaker is not None:
								# Conclude the previous speaker's block before starting the new one
								dialogue_blocks.append(f"Speaker {current_speaker}: {' '.join(current_sentence)}")
							current_speaker = display_speaker
							current_sentence = [word]
						else:
							# Append word to current speaker's ongoing sentence
							current_sentence.append(word)
					
					# Ensure the final speaker block is appended after the loop finishes
					if current_sentence:
						dialogue_blocks.append(f"Speaker {current_speaker}: {' '.join(current_sentence)}")
					
					return "\n\n".join(dialogue_blocks)
				else:
					ui.message("Error during Speech-to-Text conversion.")
					log.error(f"System Speech Translator - STT Error ({response.getcode()})")
					return None
		
		except urllib.error.HTTPError as e:
			ui.message("Error during Speech-to-Text conversion.")
			log.error(f"System Speech Translator - STT HTTP Error: {e.code} - {e.read().decode('utf-8')}")
			return None
		except Exception as e:
			ui.message("Unexpected error during audio processing.")
			log.error(f"System Speech Translator - Unexpected STT Error: {e}")
			return None
	
	def _translate(self, text):
		"""
		Takes the raw diarized text and submits it to either Google Translate (HTTP GET)
		or Grok (System Prompting) depending on the user's preference in settings.
		Uses Python's built-in urllib to remain fully GPLv2 compliant.
		"""
		engine = self.addonConf.get("translatorEngine", "Google (Free)")
		target_lang = self._get_target_language_info()
		
		if engine == "Google (Free)":
			target_code = "en"
			
			# Map the human-readable language string back to the correct API code for Google
			for name, code in LANGUAGE_CODES.items():
				if name == target_lang:
					# Google expects generic codes (e.g., 'zh' instead of 'zh-CN') except for specific dialects
					target_code = code if "zh" in code else code.split('-')[0]
					break
			
			url = "https://translate.googleapis.com/translate_a/single"
			params = {
				"client": "gtx",
				"sl": "auto",  # Source Language: Auto-detect
				"tl": target_code,
				"dt": "t",  # Return Type: Text
				"q": text
			}
			
			query_string = urllib.parse.urlencode(params)
			full_url = f"{url}?{query_string}"
			req = urllib.request.Request(full_url, method='GET')
			
			try:
				with urllib.request.urlopen(req, timeout=15) as response:
					if response.getcode() == 200:
						data = json.loads(response.read().decode('utf-8'))
						# Google returns text chunked in nested arrays. Reconstruct it into a single string.
						translated_text = "".join([chunk[0] for chunk in data[0] if chunk[0]])
						return translated_text.strip()
					else:
						ui.message("Google Translate error.")
						log.error(f"System Speech Translator - Google Translate Error ({response.getcode()})")
						return None
			except urllib.error.HTTPError as e:
				ui.message("Google Translate error.")
				log.error(
					f"System Speech Translator - Google Translate HTTP Error: {e.code} - {e.read().decode('utf-8')}")
				return None
			except Exception as e:
				ui.message("Unexpected error during Google translation.")
				log.error(f"System Speech Translator - Unexpected Google Translate Error: {e}")
				return None
		
		else:
			# GROK PREMIUM ENGINE
			# Uses the LLM endpoint (grok-4.3) to generate contextually aware translations.
			# Highly beneficial for maintaining the "Speaker X:" structure and deducing broken transcripts.
			api_key = self.addonConf.get("apiKey", "")
			url = "https://api.x.ai/v1/chat/completions"
			headers = {
				"Authorization": f"Bearer {api_key}",
				"Content-Type": "application/json"
			}
			
			payload = {
				"model": "grok-4.3",
				"reasoning_effort": "none",  # Speed optimization: We want raw translation, not thought process
				"temperature": 0.2,  # Low temperature ensures highly deterministic, accurate translations
				"messages": [
					{
						"role": "system",
						"content": (
							f"You are a strict, highly accurate translation engine. Your ONLY objective is to translate "
							f"the user's raw audio transcription into the '{target_lang}' language.\n\n"
							f"CRITICAL RULES:\n"
							f"1. YOU MUST TRANSLATE. NEVER simply echo, repeat, or leave the original input untranslated.\n"
							f"2. The transcript is already formatted with 'Speaker X:' tags. Keep these tags intact and translate the dialogue.\n"
							f"3. Output ONLY the translated dialogue. Do not include markdown (no ```), no introductory text, no background notes.\n"
							f"4. If the source text is broken, misspelled, or messy, do your best to translate the intended semantic meaning into '{target_lang}'."
						)
					},
					{
						"role": "user",
						"content": f"TRANSLATE THE FOLLOWING TRANSCRIPT INTO {target_lang}:\n\n{text}"
					}
				]
			}
			
			req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
			
			try:
				with urllib.request.urlopen(req, timeout=120) as response:
					if response.getcode() == 200:
						data = json.loads(response.read().decode('utf-8'))
						choices = data.get('choices', [])
						
						# Safely parse the schema preventing KeyErrors if the API response unexpectedly changes
						if choices and 'message' in choices[0]:
							result = choices[0]['message'].get('content', '').strip()
							
							# Fallback guard to strip errant code block markdown if the LLM hallucinated formatting.
							if result.startswith("```"):
								result = "\n".join(result.split("\n")[1:-1])
							return result.strip()
						else:
							log.error("System Speech Translator - Unexpected JSON schema from Grok API.")
							return None
					else:
						ui.message("Error receiving translation.")
						log.error(f"System Speech Translator - Translate Error ({response.getcode()})")
						return None
			except urllib.error.HTTPError as e:
				ui.message("Error receiving translation.")
				log.error(f"System Speech Translator - Translate HTTP Error: {e.code} - {e.read().decode('utf-8')}")
				return None
			except Exception as e:
				ui.message("Unexpected error during translation.")
				log.error(f"System Speech Translator - Unexpected Translation Error: {e}")
				return None