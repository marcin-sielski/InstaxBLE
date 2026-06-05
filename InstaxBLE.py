#!/usr/bin/env python3

from math import ceil
from struct import pack, unpack_from
from time import sleep

# Try to import Types with a relative import first
try:
    from .Types import EventType, InfoType, PrinterSettings
    from . import LedPatterns
except ImportError:
    # If that fails (which it will if this file is being run directly),
    # try an absolute import instead
    from Types import EventType, InfoType, PrinterSettings
    import LedPatterns

import argparse

import simplepyble
import sys
from PIL import Image
from io import BytesIO
from threading import Event
from datetime import datetime

class InstaxBLE:
    _SECONDS_PER_BLE_WRITE = 0.2     # measured avg 0.099 s/write at 2.8m, peak 0.123 (Wide Evo); 0.2 adds ~1.6x margin
    _PRINT_OVERHEAD = 30.0           # measured: ~26 s for last batch + ejection on Wide Link
    _SERVICE_UUID     = '70954782-2d83-473d-9e5f-81e1d02d5273'
    _WRITE_CHAR_UUID  = '70954783-2d83-473d-9e5f-81e1d02d5273'
    _NOTIFY_CHAR_UUID = '70954784-2d83-473d-9e5f-81e1d02d5273'

    def __init__(
        self,
        adapter=None,
        mac_address=None,
        name=None,
        print_enabled=False,
        dummy_printer=False,
        verbose=False,
        quiet=False,
        image_path=None):
        """
        Initialize the InstaxBLE class.
        deviceAddress: if specified, will only connect to a printer with this address.
        printEnabled: by default, actual printing is disabled to prevent misprints.
        """
        self._peripheral = None

        self._quiet = quiet
        self._dummy_printer = dummy_printer
        self._type = 'link'
        self._printer_settings = PrinterSettings[self._type]['mini'] if self._dummy_printer else None
        self._chunk_size = PrinterSettings[self._type]['mini']['chunkSize'] if self._dummy_printer else 0
        self._print_enabled = print_enabled
        self._device = adapter.lower() if adapter else None
        self._device_name = name.upper() if name else None
        self._device_address = mac_address.upper() if mac_address else None
        self._image_path = image_path
        self._verbose = verbose if not self._quiet else False
        self._packets_for_printing = []
        self._pos = (0, 0, 0, 0)
        self._battery_state = 0
        self._battery_percentage = 0
        self._photos_left = 0
        self._is_charging = False
        self._image_size = (PrinterSettings[self._type]['mini']['width'], PrinterSettings[self._type]['mini']['height']) if self._dummy_printer else (0, 0)
        self._waiting_for_response = False
        self._cancelled = False
        self._print_done = Event()
        self._packets = 0
        self._file_size = 0

        adapters = simplepyble.Adapter.get_adapters()
        if len(adapters) == 0:
            if not self._quiet:
                sys.exit("No bluetooth adapters found (are they enabled?)")
            else:
                sys.exit()

        number = 0
        if len(adapters) > 1:
            self._log(f"Found multiple adapters: {', '.join([adapter.identifier() for adapter in adapters])}")
            if self._device is None:
                self._log(f"Using the first one: {adapters[0].identifier()}")
            else:
                for i in range(len(adapters)):
                    if adapters[i].identifier() == self._device:
                        number = i
                        break
                self._log(f"Using: {adapters[number].identifier()}")
        self._adapter = adapters[number]

    def _timestamp(self):
        return datetime.now().strftime('%H:%M:%S.%f')[:-3]

    def _log(self, msg):
        """ Print a timestamped debug message (verbose mode only) """
        if self._verbose:
            print(f'[{self._timestamp()}] {msg}')

    def _print(self, msg):
        """ Print a timestamped informational message (unless quiet) """
        if not self._quiet:
            print(f'[{self._timestamp()}] {msg}')

    def display_current_status(self):
        """ Display an overview of the current printer state """
        print("\nPrinter details: ")
        # print(f"Device name:         {self._printer_settings['modelName']}")
        print(f"Model:               {self._printer_settings['modelName']}")
        print(f"Photos left:         {self._photos_left}/10")
        print(f"Battery level:       {self._battery_percentage}%")
        print(f"Charging:            {self._is_charging}")
        print(f"Required image size: {self._printer_settings['width']}x{self._printer_settings['height']}px")
        if self._peripheral and hasattr(self._peripheral, 'mtu') and self._peripheral.mtu:
            print(f"MTU:                 {self._peripheral.mtu()}")
        print("")

    def _parse_printer_response(self, event, packet):
        """ Parse the response packet and print the result """
        # self._log(f"event: {event}")
        self._waiting_for_response = False

        if event == EventType.XYZ_AXIS_INFO:
            x, y, z, o = unpack_from('<hhhB', packet[6:-1])
            self._pos = (x, y, z, o)
        elif event == EventType.LED_PATTERN_SETTINGS:
            pass
        elif event == EventType.SUPPORT_FUNCTION_INFO:
            try:
                info_type = InfoType(packet[7])
            except ValueError:
                self._log(f'Unknown InfoType: {packet[7]}')
                return

            if info_type == InfoType.IMAGE_SUPPORT_INFO:
                w, h = unpack_from('>HH', packet[8:12])
                # self._log(self._prettify_bytearray(packet[8:12]))
                # self._log(f'image size: {w}x{h}')
                self._image_size = (w, h)
                if (w, h) == (600, 800):
                    self._printer_settings = PrinterSettings[self._type]['mini']
                elif (w, h) == (800, 800):
                    self._printer_settings = PrinterSettings[self._type]['square']
                elif (w, h) == (1260, 840):
                    self._printer_settings = PrinterSettings[self._type]['wide']
                else:
                    exit(f'Unknown image size from printer: {w}x{h}')

                self._chunk_size = self._printer_settings['chunkSize']
                self._file_size = int(float(unpack_from('>I',packet[14:18])[0])/1024)
                # self._log(f"Max file size for this printer: {self._file_size} KB")

            elif info_type == InfoType.BATTERY_INFO:
                self._battery_state, self._battery_percentage = unpack_from('>BB', packet[8:10])
                # self._log(f'battery state: {self._battery_state}, battery percentage: {self._battery_percentage}')
            elif info_type == InfoType.PRINTER_FUNCTION_INFO:
                data_byte = packet[8]
                self._photos_left = data_byte & 15
                self._is_charging = (1 << 7) & data_byte >= 1
                # self._log(f'photos left: {self._photos_left}')
                # if self._is_charging:
                #     self._log('Printer is charging')
                # else:
                #     self._log('Printer is running on battery')

        elif event == EventType.PRINT_IMAGE_DOWNLOAD_START:
            # self._log(self._prettify_bytearray(packet))
            self._handle_image_packet_queue()

        elif event == EventType.PRINT_IMAGE_DOWNLOAD_DATA:
            self._handle_image_packet_queue()

        elif event == EventType.PRINT_IMAGE_DOWNLOAD_END:
            self._handle_image_packet_queue()

        elif event == EventType.PRINT_IMAGE_DOWNLOAD_CANCEL:
            self._print_done.set()

        elif event == EventType.PRINT_IMAGE:
            self._log('received print confirmation')
            self._print_done.set()

        else:
            self._log(f'Uncaught response from printer. Eventype: {event}')

    def _handle_image_packet_queue(self):
        if len(self._packets_for_printing) > 0 and not self._cancelled:
            if len(self._packets_for_printing) % 10 == 0:
                self._log(f"Img packets left to send: {len(self._packets_for_printing)}")
                sys.stdout.write("Printing progress: %d%%   \r" % ((self._packets-len(self._packets_for_printing))*100/self._packets))
                sys.stdout.flush()
            packet = self._packets_for_printing.pop(0)
            self._send_packet(packet)

    def _notification_handler(self, packet):
        """ Gets called whenever the printer replies and handles parsing the received data """
        # self._log('Notification handler:')
        # self._log(f'\t{self._prettify_bytearray(packet[:40])}')
        if not self._quiet:
            if len(packet) < 8:
                self._log(f"\tError: response packet size should be >= 8 (was {len(packet)})!")
                return
            elif not self._validate_checksum(packet):
                self._log("\tResponse packet checksum was invalid!")
                return

        header, length, op1, op2 = unpack_from('>HHBB', packet)
        # self._log('\theader: ', header, '\t', self._prettify_bytearray(packet[0:2]))
        # self._log('\tlength: ', length, '\t', self._prettify_bytearray(packet[2:4]))
        # self._log('\top1: ', op1, '\t\t', self._prettify_bytearray(packet[4:5]))
        # self._log('\top2: ', op2, '\t\t', self._prettify_bytearray(packet[5:6]))

        try:
            event = EventType((op1, op2))
            # self._log(f'\tResponse event: {event}')
        except ValueError:
            self._log(f"Unknown EventType: ({op1}, {op2})")
            return

        self._parse_printer_response(event, packet)

    def connect(self, timeout=0) -> bool:
        """ Connect to the printer. Stops trying after <timeout> seconds.
            Returns True if fully connected and ready, False otherwise. """
        if self._dummy_printer:
            return True

        self._peripheral = self._find_device(timeout=timeout)
        if self._peripheral:
            try:
                self._log(f"Connecting to {self._peripheral.identifier()} [{self._peripheral.address()}]")
                self._peripheral.connect()
            except Exception as e:
                if not self._quiet:
                    self._log(f'error on connecting: {e}')
                return False

            if self._peripheral.is_connected():
                # check if we're using a version of simplepyble that supports reading mtu
                self._log(f"Connected")

                # self._log('Attaching _notification_handler')
                try:
                    self._peripheral.notify(self._SERVICE_UUID, self._NOTIFY_CHAR_UUID, self._notification_handler)
                except Exception as e:
                    if not self._quiet:
                        self._log(f'Error on attaching _notification_handler: {e}')
                    self.disconnect()
                    return False

                self.get_printer_info()
                sleep(1)
                self.display_current_status()
                return True
        return False

    def disconnect(self):
        """ Disconnect from the printer (if connected) """
        if self._dummy_printer:
            return
        if self._peripheral:
            if self._peripheral.is_connected():
                # if len(self._packets_for_printing) > 0 and not self._cancelled:
                #     self._log('sending cancel command')
                #     self._send_packet(self._create_packet(EventType.PRINT_IMAGE_DOWNLOAD_CANCEL))
                self._log('Disconnecting...')
                self._peripheral.disconnect()
                self._log("Disconnected")

    def cancel_print(self):
        self._packets_for_printing = []
        self._packets = 0
        self._waiting_for_response = False
        self._send_packet(self._create_packet(EventType.PRINT_IMAGE_DOWNLOAD_CANCEL))

    def enable_printing(self):
        """ Enable printing. """
        self._print_enabled = True

    def disable_printing(self):
        """ Disable printing. """
        self._print_enabled = False

    def _find_device(self, timeout=0):
        """" Scan for our device and return it when found """
        self._log('Searching for instax printer...')
        seconds_tried = 0
        try:
            while True:
                self._adapter.scan_for(2000)
                peripherals = self._adapter.scan_get_results()
                for peripheral in peripherals:
                    found_name = peripheral.identifier()
                    found_address = peripheral.address()
                    # if found_name.startswith('INSTAX'):
                    #     self._log(f"Found: {found_name} [{found_address}]")
                    if (self._device_name and found_name.startswith(self._device_name)) or \
                       (self._device_address and found_address == self._device_address) or \
                       (self._device_name is None and self._device_address is None and
                       found_name.startswith('INSTAX-') and (found_name.endswith('(IOS)') or \
                       found_name.endswith('(ANDROID)') or found_name.endswith('(BLE)'))):
                        # if found_address.startswith('FA:AB:BC'):  # start of IOS endpooint
                        #     to convert to ANDROID endpoint, replace 'FA:AB:BC' with '88:B4:36')
                        if peripheral.is_connectable():
                            if found_name.endswith('(BLE)'):
                                self._type = 'evo'
                            return peripheral
                        elif not self._quiet:
                            self._log(f"Can't connect to printer at {found_address}")
                seconds_tried += 2
                if timeout != 0 and seconds_tried >= timeout:
                    return None
        except KeyboardInterrupt:
            self.cancel_print()
            self.disconnect()
            sys.exit()

    def _create_color_payload(self, color_array, speed, repeat, when):
        """
        Create a payload for a color pattern. See send_led_pattern for details.
        """
        payload = pack('BBBB', when, len(color_array), speed, repeat)
        for color in color_array:
            payload += pack('BBB', color[0], color[1], color[2])
        return payload

    def send_led_pattern(self, pattern, speed=5, repeat=255, when=0):
        """ Send a LED pattern to the Instax printer.
            color_array: array of BGR(!) values to use in animation, e.g. [[255, 0, 0], [0, 255, 0], [0, 0, 255]]
            speed: time per frame/color: higher is slower animation
            repeat: 0 = don't repeat (so play once), 1-254 = times to repeat, 255 = repeat forever
            when: 0 = normal, 1 = on print, 2 = on print completion, 3 = pattern switch """
        if not self._peripheral or self._peripheral.identifier().endswith('(BLE)'):
            return
        payload = self._create_color_payload(pattern, speed, repeat, when)
        packet = self._create_packet(EventType.LED_PATTERN_SETTINGS, payload)
        self._send_packet(packet)

    def _prettify_bytearray(self, value):
        """ Helper funtion to convert a bytearray to a string of hex values. """
        return ' '.join([f'{x:02x}' for x in value])

    def _create_checksum(self, bytearray):
        """ Create a checksum for a given packet. """
        return (255 - (sum(bytearray) & 255)) & 255

    def _create_packet(self, event_type, payload=b''):
        """ Create a packet to send to the printer. """
        if isinstance(event_type, EventType):  # allows passing in an event or a value directly
            event_type = event_type.value

        header = b'\x41\x62'  # 'Ab' means client to printer, 'aB' means printer to client
        opCode = bytes([event_type[0], event_type[1]])
        packetSize = pack('>H', 7 + len(payload))
        packet = header + packetSize + opCode + payload
        packet += pack('B', self._create_checksum(packet))
        return packet

    def _validate_checksum(self, packet):
        """ Validate the checksum of a packet. """
        return (sum(packet) & 255) == 255

    def _send_packet(self, packet):
        """ Send a packet to the printer """
        if not self._dummy_printer and not self._quiet:
            if not self._peripheral:
                self._log("no peripheral to send packet to")
            elif not self._peripheral.is_connected():
                self._log("peripheral not connected")

        try:
            while self._waiting_for_response and not self._dummy_printer and not self._cancelled:
                # self._log("sleep")
                sleep(0.05)

            header, length, op1, op2 = unpack_from('>HHBB', packet)
            try:
                event = EventType((op1, op2))
            except Exception:
                event = 'Unknown event'

            # self._log(f'sending eventtype: {event}')

            self._waiting_for_response = True
            small_packet_size = 182
            num_parts = ceil(len(packet) / small_packet_size)
            # self._log(f"> number of parts to send: {num_parts}")
            for sub_part_index in range(num_parts):
                # self._log((sub_part_index + 1), '/', num_parts)
                sub_packet = packet[sub_part_index * small_packet_size:sub_part_index * small_packet_size + small_packet_size]

                if not self._dummy_printer:
                    self._peripheral.write_command(self._SERVICE_UUID, self._WRITE_CHAR_UUID, sub_packet)

        except KeyboardInterrupt:
            self._cancelled = True
            self.cancel_print()
            # sleep(1)
            self.disconnect()
            sys.exit('Cancelled')

    def print_image(self, img_src):
        """
        print an image. Either pass a path to an image (as a string) or pass
        the bytearray to print directly
        """
        self._log(f'printing image "{img_src}"')
        if self._photos_left == 0 and not self._dummy_printer:
            self._log("Can't print: no photos left")
            return

        img_data = img_src
        if isinstance(img_src, str):  # if it's a path, load the image contents
            image = Image.open(img_src)
            img_data = self._pil_image_to_bytes(image, max_size_kb=self._file_size)
        elif isinstance(img_src, BytesIO):
            img_src.seek(0)  # Go to the start of the BytesIO object
            image = Image.open(img_src)
            img_data = self._pil_image_to_bytes(image, max_size_kb=self._file_size)

        # self._log(f"len of imagedata: {len(img_data)}")
        self._packets_for_printing = [
            # \x02\x00\x00\x00 payload made of four bytes: pictureType, picturePrintOption, picturePrintOption2, zero
            self._create_packet(EventType.PRINT_IMAGE_DOWNLOAD_START, b'\x02\x00\x00\x00' + pack('>I', len(img_data)))
        ]

        # divide image data up into chunks of <chunkSize> bytes and pad the last chunk with zeroes if needed
        img_data_chunks = [img_data[i:i + self._chunk_size] for i in range(0, len(img_data), self._chunk_size)]
        if len(img_data_chunks[-1]) < self._chunk_size:
            img_data_chunks[-1] = img_data_chunks[-1] + bytes(self._chunk_size - len(img_data_chunks[-1]))

        # create a packet from each of our chunks, this includes adding the chunk number
        for index, chunk in enumerate(img_data_chunks):
            img_data_chunks[index] = pack('>I', index) + chunk  # add chunk number as int (4 bytes)
            self._packets_for_printing.append(self._create_packet(EventType.PRINT_IMAGE_DOWNLOAD_DATA, img_data_chunks[index]))

        self._packets_for_printing.append(self._create_packet(EventType.PRINT_IMAGE_DOWNLOAD_END))

        if self._print_enabled:
            self._packets_for_printing.append(self._create_packet(EventType.PRINT_IMAGE))
            self._packets_for_printing.append(self._create_packet((0, 2), b'\x02'))
        elif not self._quiet:
            self._log("Printing is disabled, sending all packets except the actual print command")

        # for packet in self._packets_for_printing:
        #     self._log(self._prettify_bytearray(packet))
        # exit()
        # send the first packet from our list, the packet handler will take care of the rest
        self._packets = len(self._packets_for_printing)
        if not self._dummy_printer:
            packet = self._packets_for_printing.pop(0)
            self._print_done.clear()
            self._send_packet(packet)
            # try:
            #     while len(self._packets_for_printing) > 0:
            #         sleep(0.1)
            # except KeyboardInterrupt:
            #     self._cancelled = True
            #     self.disconnect()
            #     sys.exit('Cancelled')

    def _print_services(self):
        """ Get and display and overview of the printer's services and characteristics """
        if not self._peripheral:
            return
        self._log("Successfully connected, listing services...")
        services = self._peripheral.services()
        service_characteristic_pair = []
        for service in services:
            for characteristic in service.characteristics():
                service_characteristic_pair.append((service.uuid(), characteristic.uuid()))

        for i, (service_uuid, characteristic) in enumerate(service_characteristic_pair):
            self._log(f"{i}: {service_uuid} {characteristic}")

    def get_printer_orientation(self):
        """ Get the current XYZ orientation of the printer """
        packet = self._create_packet(EventType.XYZ_AXIS_INFO)
        self._send_packet(packet)

    def get_printer_status(self):
        """ Get the printer's status"""
        packet = self._create_packet(EventType.SUPPORT_FUNCTION_INFO, pack('>B', InfoType.PRINTER_FUNCTION_INFO.value))
        self._send_packet(packet)

    def get_printer_info(self):
        """ Get and display the printer's status and info, like photos left and battery level """
        # self._log("Getting function info...")

        packet = self._create_packet(EventType.SUPPORT_FUNCTION_INFO, pack('>B', InfoType.IMAGE_SUPPORT_INFO.value))
        self._send_packet(packet)

        packet = self._create_packet(EventType.SUPPORT_FUNCTION_INFO, pack('>B', InfoType.BATTERY_INFO.value))
        self._send_packet(packet)

        self.get_printer_status()

    def _pil_image_to_bytes(self, img: Image.Image, max_size_kb: int = None) -> bytearray:
        """ Convert a PIL image to a bytearray """
        img_buffer = BytesIO()

        # Convert the image to RGB mode if it's not already (e.g. RGBA, L, P, CMYK)
        if img.mode != 'RGB':
            img = img.convert('RGB')

        # Rotate image to match printer orientation (skip for square printers)
        target_w, target_h = self._image_size
        if target_w != target_h and (target_w > target_h) != (img.width > img.height):
            img = img.rotate(-90, expand=True)

        # Scale to fill the physical print area (cover mode), then pad hidden strips with white
        top_strip = self._printer_settings['topStrip']
        bottom_strip = self._printer_settings['bottomStrip']
        physical_h = target_h - top_strip - bottom_strip
        src_ar = img.width / img.height
        phys_ar = target_w / physical_h
        if src_ar >= phys_ar:
            new_w = round(physical_h * src_ar)
            new_h = physical_h
        else:
            new_w = target_w
            new_h = round(target_w / src_ar)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - target_w) // 2
        top = (new_h - physical_h) // 2
        img = img.crop((left, top, left + target_w, top + physical_h))
        if top_strip > 0 or bottom_strip > 0:
            canvas = Image.new('RGB', (target_w, target_h), (255, 255, 255))
            canvas.paste(img, (0, top_strip))
            img = canvas

        def save_img_with_quality(quality):
            img_buffer.seek(0)
            img_buffer.truncate(0)
            img.save(img_buffer, format='JPEG', quality=quality)
            return img_buffer.tell() / 1024

        if max_size_kb is not None:
            low_quality, high_quality = 1, 100
            current_quality = 75
            closest_quality = current_quality
            min_target_size_kb = max_size_kb * 0.9

            while low_quality <= high_quality:
                output_size_kb = save_img_with_quality(current_quality)
                # self._log(f"current output quality: {current_quality}, current size: {output_size_kb}")

                if output_size_kb <= max_size_kb and output_size_kb >= min_target_size_kb:
                    closest_quality = current_quality
                    break

                if output_size_kb > max_size_kb:
                    high_quality = current_quality - 1
                else:
                    low_quality = current_quality + 1

                current_quality = (low_quality + high_quality) // 2
                closest_quality = current_quality

            # Save the image with the closest_quality
            save_img_with_quality(closest_quality)
            self._log(f'Saved img with quality of {closest_quality}')
        else:
            img.save(img_buffer, format='JPEG')

        return bytearray(img_buffer.getvalue())

    def wait_until_image_is_printed(self, timeout: float = None) -> bool:
        """ Wait until image is printed. Returns False if timed out (e.g. printer disconnected).
            timeout: seconds to wait; if None, derived from queued packet count. """
        self._print("Waiting until image is printed...")
        if not self._dummy_printer:
            if timeout is None:
                timeout = (self._file_size * 1024 / 182) * self._SECONDS_PER_BLE_WRITE + self._PRINT_OVERHEAD
            completed = self._print_done.wait(timeout=timeout)
            if not completed:
                self._print("Warning: timed out waiting for print confirmation")
            return completed
        return True


def main(args={}):
    """ Example usage of the InstaxBLE class """
    instax = InstaxBLE(**args)
    try:
        # To prevent misprints during development this script sends all the
        # image data except the final 'go print' command. To enable printing
        # uncomment the next line, or pass --print-enabled when calling
        # this script

        # instax.enable_printing()
        if not instax.connect():
            return
        # Set a rainbow effect to be shown while printing and a pulsating
        # green effect when printing is done
        instax.send_led_pattern(LedPatterns.rainbow, when=1)
        instax.send_led_pattern(LedPatterns.pulseGreen, when=2)
        # you can also read the current accelerometer values if you want
        # while True:
        #     instax.get_printer_orientation()
        #     sleep(.5)

        # send your image (.jpg) to the printer by
        # passing the image_path as an argument when calling
        # this script, or by specifying the path in your code
        if instax._image_path:
            instax.print_image(instax._image_path)
        else:
            instax.print_image(instax._printer_settings['exampleImage'])
        instax.wait_until_image_is_printed()

    except Exception as e:
        print(type(e).__name__, __file__, e.__traceback__.tb_lineno)
        instax._log(f'Error: {e}')
    finally:
        print('finally, disconnect')
        instax.disconnect()  # all done, disconnect


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', '--adapter')
    parser.add_argument('-m', '--mac-address')
    parser.add_argument('-n', '--name')
    parser.add_argument('-p', '--print-enabled', action='store_true')
    parser.add_argument('-d', '--dummy-printer', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-q', '--quiet', action='store_true')
    parser.add_argument('-i', '--image-path', help='Path to the image file')
    args = parser.parse_args()

    main(vars(args))
