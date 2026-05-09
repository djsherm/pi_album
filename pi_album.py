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

from PIL import Image

import os
import io
import time
from typing import Optional, Tuple
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from PIL import Image
from typing import Optional, Tuple, Dict

"""
SET UP PATH VARIABLES, INKY, AND GET PHOTOS
"""

PATH = '/home/daniel/Pimoroni/inky/examples/7color'
print('path:', PATH)
JEOPARDY_PATH = os.path.join(PATH, "jeopardy")

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

TARGET_SIZE = CANVAS_SIZE = (640, 400)
JPEG_QUALITY = 90

from typing import Tuple
import io
import os
from PIL import Image

JPEG_QUALITY = 90
CANVAS_SIZE = (640, 400)

def _save_png_bytes_to_jpg_with_padding(
    png_bytes: bytes,
    out_path: str,
    target_size: Tuple[int, int] = CANVAS_SIZE,
) -> None:
    """
    Convert PNG bytes to a JPEG at `out_path`, scaling the image so its height is 400px
    (preserving aspect ratio), centering it horizontally, and adding white padding
    so the final image is 640x400.

    This version is defensive: ensures directory exists, validates bytes, converts to RGB,
    and logs/surfaces errors rather than silently failing.
    """
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise TypeError("png_bytes must be bytes or bytearray")

    # Ensure parent directory exists
    parent = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(parent, exist_ok=True)

    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            # Work in RGBA so transparency is respected if present
            im = im.convert("RGBA")
            canvas_w, canvas_h = target_size

            iw, ih = im.size
            if ih == 0:
                raise ValueError("Source image has zero height")

            # Scale so height equals canvas height (400)
            scale = canvas_h / ih
            new_w = max(1, int(round(iw * scale)))
            new_h = canvas_h

            resized = im.resize((new_w, new_h), Image.LANCZOS)

            # Create white background and paste centered horizontally
            bg = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
            paste_x = (canvas_w - new_w) // 2
            paste_y = 0  # heights match, so top = 0

            # Paste with alpha mask (handles transparency)
            bg.paste(resized, (paste_x, paste_y), resized)

            # Ensure RGB (JPEG) and save
            rgb = bg.convert("RGB")
            rgb.save(out_path, format="JPEG", quality=JPEG_QUALITY)
    except Exception:
        # Reraise but annotate to make debugging easier
        raise RuntimeError(f"Failed to save image to {out_path}") from None
def save_jarchive_questions_and_answers(
    out_dir: str,
    chromium_path: Optional[str] = None,
    chromedriver_path: Optional[str] = None,
    headless: bool = True,
    wait_seconds: int = 12,
) -> Tuple[
    Tuple[str, Dict[str, str]],
    Tuple[str, Dict[str, str]]
]:
    """
    Save Recent/Random question images and corresponding answer images (same size/format).

    Returns:
      ((recent_question_path, recent_meta), (random_question_path, random_meta))
      where meta = {"category": "...", "clue": "...", "answer": "..."}
    """
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(out_dir):
        raise FileNotFoundError(f"Directory does not exist: {out_dir}")

    recent_q = os.path.join(out_dir, "recent.jpg")
    random_q = os.path.join(out_dir, "random.jpg")
    recent_a = os.path.join(out_dir, "recent_answer.jpg")
    random_a = os.path.join(out_dir, "random_answer.jpg")

    # Common default paths (adjust if necessary)
    default_chromium_bins = ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/snap/bin/chromium"]
    default_chromedriver_bins = ["/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver", "/usr/local/bin/chromedriver"]

    chromium = chromium_path or next((p for p in default_chromium_bins if os.path.isfile(p)), None)
    chromedriver = chromedriver_path or next((p for p in default_chromedriver_bins if os.path.isfile(p)), None)

    if chromium is None:
        raise FileNotFoundError("Chromium binary not found. Install chromium-browser or set chromium_path.")
    if chromedriver is None:
        raise FileNotFoundError("chromedriver binary not found. Install chromium-chromedriver or set chromedriver_path.")

    options = Options()
    if headless:
        try:
            options.add_argument("--headless=new")
        except Exception:
            options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # Window size can be larger than needed; screenshots are of the element itself.
    options.add_argument("--window-size=1600,1400")
    options.binary_location = chromium

    service = Service(chromedriver)
    driver = None

    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.get("https://www.j-archive.com/")

        wait = WebDriverWait(driver, wait_seconds)
        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table.final_round")))
        except TimeoutException:
            # continue best-effort
            pass

        tables = driver.find_elements(By.CSS_SELECTOR, "table.final_round")
        if not tables:
            raise Exception("No 'table.final_round' elements found on the page")

        def extract_meta_from_table(elem) -> Dict[str, str]:
            cat = ""
            try:
                cat = elem.find_element(By.CSS_SELECTOR, "td.category_name").text.strip()
            except Exception:
                cat = ""
            clue = ""
            try:
                clue_cells = elem.find_elements(By.CSS_SELECTOR, "td.clue_text")
                for c in clue_cells:
                    try:
                        if c.is_displayed() and c.text.strip():
                            clue = c.text.strip()
                            break
                    except Exception:
                        pass
                if not clue and clue_cells:
                    clue = clue_cells[0].text.strip()
            except Exception:
                clue = ""
            return {"category": cat, "clue": clue, "answer": ""}

        def capture_question(elem, out_path) -> Dict[str, str]:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", elem)
            time.sleep(0.2)
            png = elem.screenshot_as_png
            # Ensure we request the canonical (640x400) canvas
            _save_png_bytes_to_jpg_with_padding(png, out_path, target_size=CANVAS_SIZE)
            return extract_meta_from_table(elem)

        def reveal_answer_and_capture(elem, out_path) -> str:
            answer_cells = elem.find_elements(By.CSS_SELECTOR, "td.clue_text[id$='_r']")
            if not answer_cells:
                all_clues = elem.find_elements(By.CSS_SELECTOR, "td.clue_text")
                for c in all_clues:
                    try:
                        inner = c.get_attribute("innerHTML") or ""
                        if "correct_response" in inner:
                            answer_cells = [c]
                            break
                    except Exception:
                        continue

            if not answer_cells:
                all_clues = elem.find_elements(By.CSS_SELECTOR, "td.clue_text")
                for c in all_clues:
                    try:
                        style = (c.get_attribute("style") or "").replace(" ", "")
                        if "display:none" in style or not c.is_displayed():
                            answer_cells = [c]
                            break
                    except Exception:
                        continue

            if not answer_cells:
                raise Exception("Answer cell not found inside the provided final_round table")

            ans_elem = answer_cells[0]

            driver.execute_script("""
                var el = arguments[0];
                el.style.display = 'table-cell';
                el.style.visibility = 'visible';
                el.style.opacity = '1';
                var em = el.querySelector('em.correct_response');
                if (em) { em.style.display = 'inline'; em.style.visibility = 'visible'; }
            """, ans_elem)

            time.sleep(0.15)

            parent_table = elem
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", parent_table)
            time.sleep(0.15)
            png = parent_table.screenshot_as_png
            _save_png_bytes_to_jpg_with_padding(png, out_path, target_size=CANVAS_SIZE)

            try:
                revealed = ans_elem.text.strip()
            except Exception:
                revealed = (ans_elem.get_attribute("innerText") or "").strip()
            return revealed

        if len(tables) >= 2:
            recent_table = tables[0]
            random_table = tables[1]
        else:
            recent_table = random_table = tables[0]

        recent_meta = capture_question(recent_table, recent_q)
        random_meta = capture_question(random_table, random_q)

        recent_answer_text = reveal_answer_and_capture(recent_table, recent_a)
        random_answer_text = reveal_answer_and_capture(random_table, random_a)

        recent_meta["answer"] = recent_answer_text
        random_meta["answer"] = random_answer_text

        return (recent_q, recent_meta), (random_q, random_meta)

    except WebDriverException as e:
        raise Exception(f"Selenium/WebDriver error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass



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
	print(inky.resolution)
	
	displayImage = resizedImage.rotate(180)
	
	inky.set_image(displayImage, saturation=0.5)
	inky.show()
	
	return None

def Show_Image_On_Pimoroni(inky, path):
    
    """
    inky = inky object representing the eInk display
    path = path to the actual image
    """
    image = Image.open(path)
    inky.set_image(image.rotate(180), saturation=0.5)
    inky.show()


# "handle_button" will be called every time a button is pressed
# It receives one argument: the associated input pin.
def handle_button(pin):
	global inky
	global photos
	label = LABELS[BUTTONS.index(pin)]
	print("Button press detected on pin: {} label: {}".format(pin, label))
	if pin == 5: # button furthest away from usb power plug
		Show_Image(inky, 'album', photos) # shows new image every time button A is pressed
	if pin == 6:
		Show_Image_On_Pimoroni(inky, os.path.join("/home/daniel/Pimoroni/inky/examples/7color/jeopardy", "random.jpg"))
	if pin == 16:
		Show_Image_On_Pimoroni(inky, os.path.join("/home/daniel/Pimoroni/inky/examples/7color/jeopardy", "random_answer.jpg"))
	# can add more functionality for other buttons here

'''----------STARTUP FUNCTIONS----------'''
# upon startup, scrape and save today's final jeopardy
# if the image already exists, delete it
if os.path.exists(os.path.join(JEOPARDY_PATH, "random.jpg")):
    print(f"{os.path.join(JEOPARDY_PATH, 'random.jpg')} exists, overwriting.")
    os.remove(os.path.join(JEOPARDY_PATH, "random.jpg"))
if os.path.exists(os.path.join(JEOPARDY_PATH, "recent.jpg")):
    print(f"{os.path.join(JEOPARDY_PATH, 'recent.jpg')} exists, overwriting.")
    os.remove(os.path.join(JEOPARDY_PATH, "recent.jpg"))
if os.path.exists(os.path.join(JEOPARDY_PATH, "random_answer.jpg")):
    print(f"{os.path.join(JEOPARDY_PATH, 'random_answer.jpg')} exists, overwriting.")
    os.remove(os.path.join(JEOPARDY_PATH, "random_answer.jpg"))
if os.path.exists(os.path.join(JEOPARDY_PATH, "recent_answer.jpg")):
    print(f"{os.path.join(JEOPARDY_PATH, 'recent_answer.jpg')} exists, overwriting.")
    os.remove(os.path.join(JEOPARDY_PATH, "recent_answer.jpg"))
    
print("Starting to scrape Jeopardy questions")
save_jarchive_questions_and_answers(JEOPARDY_PATH)
Show_Image(inky, 'album', photos) # should display random image on startup, and give availability to show new image with button
print("Scraping images complete!")
'''-----------------'''

# plot_path = wrapper()
# plot = Image.open(plot_path)
# resizedPlot = plot.resize(inky.resolution)
# displayPlot = resizedPlot.rotate(180)
# inky.set_image(displayPlot, saturation=0.5)
# inky.show()
# Loop through out buttons and attach the "handle_button" function to each
# We're watching the "FALLING" edge (transition from 3.3V to Ground) and
# picking a generous bouncetime of 250ms to smooth out button presses.
for pin in BUTTONS:
    GPIO.add_event_detect(pin, GPIO.FALLING, handle_button, bouncetime=250)

# Finally, since button handlers don't require a "while True" loop,
# we pause the script to prevent it exiting immediately.
signal.pause()
