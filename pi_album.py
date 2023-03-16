#!/usr/bin/env python3

"""
LIBRARY IMPORTS
"""

import sys
import os
from PIL import Image
from inky.auto import auto
import random
import signal
import RPi.GPIO as GPIO

"""
SET UP PATH VARIABLES, INKY, AND GET PHOTOS
"""

PATH = '/home/daniel/Pimoroni/inky/examples/7color'
print('path:', PATH)

photo_album = os.path.join(PATH, 'album')
photos = os.listdir(photo_album)
print(photos)

inky = auto(ask_user=True, verbose=True)
saturation = 0.5

# Gpio pins for each button (from top to bottom)
BUTTONS = [5, 6, 16, 24]

# These correspond to buttons A, B, C and D respectively
LABELS = ['A', 'B', 'C', 'D']

# Set up RPi.GPIO with the "BCM" numbering scheme
GPIO.setmode(GPIO.BCM)

# Buttons connect to ground when pressed, so we should set them up
# with a "PULL UP", which weakly pulls the input signal to 3.3V.
GPIO.setup(BUTTONS, GPIO.IN, pull_up_down=GPIO.PUD_UP)

"""
CHOOSE RANDOM PHOTO TO DISPLAY
"""
def Show_Image(inky, path, album):
	"""
	inky = inky object representing the eInk display
	path = path to the album the photos are in
	album = list containing strings of photo names
	"""
	
	selected_image = random.randint(0, len(album) - 1)
	print(album[selected_image])
	image = Image.open(os.path.join('/home/daniel/Pimoroni/inky/examples/7color', path, album[selected_image]))
	resizedImage = image.resize(inky.resolution)
	
	inky.set_image(resizedImage, saturation=0.5)
	inky.show()
	
	return None
	
# "handle_button" will be called every time a button is pressed
# It receives one argument: the associated input pin.
def handle_button(pin):
	global inky
	global photos
	label = LABELS[BUTTONS.index(pin)]
	print("Button press detected on pin: {} label: {}".format(pin, label))
	if pin == 5:
		Show_Image(inky, 'album', photos) # shows new image every time button A is pressed
	# can add more functionality for other buttons here


Show_Image(inky, 'album', photos) # should display random image on startup, and give availability to show new image with button

# Loop through out buttons and attach the "handle_button" function to each
# We're watching the "FALLING" edge (transition from 3.3V to Ground) and
# picking a generous bouncetime of 250ms to smooth out button presses.
for pin in BUTTONS:
    GPIO.add_event_detect(pin, GPIO.FALLING, handle_button, bouncetime=250)

# Finally, since button handlers don't require a "while True" loop,
# we pause the script to prevent it exiting immediately.
signal.pause()
