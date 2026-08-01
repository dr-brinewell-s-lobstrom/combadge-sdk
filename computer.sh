# Server launcher.  Small model always; large model too if it is there.
#
# The large model (~1.8 GB) enables dictation — "captain's log" and
# "computer transcribe", README §11.  It is passed ONLY when the directory
# exists, so this stays a one-command start whether or not you have
# downloaded it: no large model, no dictation, everything else unchanged.
#
# computer.py exits on a large-model path that does not resolve, which is
# what you want from a typo on a command line but not from a launcher
# shipped to people who may never fetch that model — hence the test.
SMALL=../.vosk/vosk-model-small-en-us-0.15
LARGE=../.vosk/vosk-model-en-us-0.22

if [ -d "$LARGE" ]; then
    python computer.py "$SMALL" "$LARGE"
else
    python computer.py "$SMALL"
fi
