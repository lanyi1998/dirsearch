# -*- coding: utf-8 -*-
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#  Author: Mauro Soria

import threading
import time

from lib.connection.request_exception import RequestException
from .path import *
from .scanner import *


class Fuzzer(object):
    def __init__(
        self,
        requester,
        dictionary,
        suffixes=None,
        prefixes=None,
        excludeContent=None,
        threads=1,
        delay=0,
        maxrate=0,
        matchCallbacks=[],
        notFoundCallbacks=[],
        errorCallbacks=[],
    ):

        self.requester = requester
        self.dictionary = dictionary
        self.suffixes = suffixes if suffixes else []
        self.prefixes = prefixes if prefixes else []
        self.excludeContent = excludeContent
        self.basePath = self.requester.basePath
        self.threads = []
        self.threadsCount = (
            threads if len(self.dictionary) >= threads else len(self.dictionary)
        )
        self.delay = delay
        self.maxrate = maxrate
        self.running = False
        self.stopped = 0
        self.calibration = None
        self.defaultScanner = None
        self.matchCallbacks = matchCallbacks
        self.notFoundCallbacks = notFoundCallbacks
        self.errorCallbacks = errorCallbacks
        self.matches = []
        self.scanners = {
            "prefixes": {},
            "suffixes": {},
        }

    def wait(self, timeout=None):
        for thread in self.threads:
            thread.join(timeout)

            if timeout and thread.is_alive():
                return False

        return True

    def setupScanners(self):
        if len(self.scanners):
            self.scanners = {
                "prefixes": {},
                "suffixes": {},
            }

        # Default scanners (wildcard testers)
        self.defaultScanner = Scanner(self.requester)
        self.prefixes.append(".")
        self.suffixes.append("/")

        for prefix in self.prefixes:
            self.scanners["prefixes"][prefix] = Scanner(
                self.requester, prefix=prefix
            )

        for suffix in self.suffixes:
            self.scanners["suffixes"][suffix] = Scanner(
                self.requester, suffix=suffix
            )

        for extension in self.dictionary.extensions:
            if "." + extension not in self.scanners["suffixes"]:
                self.scanners["suffixes"]["." + extension] = Scanner(
                    self.requester, suffix="." + extension
                )

        if self.excludeContent:
            if self.excludeContent.startswith("/"):
                self.excludeContent = self.excludeContent[1:]
            self.calibration = Scanner(self.requester, calibration=self.excludeContent)

    def setupThreads(self):
        if len(self.threads):
            self.threads = []

        for thread in range(self.threadsCount):
            newThread = threading.Thread(target=self.thread_proc)
            newThread.daemon = True
            self.threads.append(newThread)

    def getScannerFor(self, path):
        # Clean the path, so can check for extensions/suffixes
        path = path.split("?")[0].split("#")[0]

        if self.excludeContent:
            yield self.calibration

        for prefix in self.prefixes:
            if path.startswith(prefix):
                yield self.scanners["prefixes"][prefix]

        for suffix in self.suffixes:
            if path.endswith(suffix):
                yield self.scanners["suffixes"][suffix]

        for extension in self.dictionary.extensions:
            if path.endswith("." + extension):
                yield self.scanners["suffixes"]["." + extension]

        yield self.defaultScanner

    def start(self):
        # Setting up testers
        self.setupScanners()
        # Setting up threads
        self.setupThreads()
        self.index = 0
        self.rate = 0
        self.dictionary.reset()
        self.runningThreadsCount = len(self.threads)
        self.running = True
        self.paused = False
        self.playEvent = threading.Event()
        self.pausedSemaphore = threading.Semaphore(0)
        self.playEvent.clear()
        self.exit = False

        for thread in self.threads:
            thread.start()

        self.play()

    def play(self):
        self.playEvent.set()

    def pause(self):
        self.paused = True
        self.playEvent.clear()
        for thread in self.threads:
            if thread.is_alive():
                self.pausedSemaphore.acquire()

    def resume(self):
        self.paused = False
        self.pausedSemaphore.release()
        self.play()

    def stop(self):
        self.running = False
        self.play()

    def scan(self, path):
        response = self.requester.request(path)
        result = response.status

        for tester in list(set(self.getScannerFor(path))):
            if not tester.scan(path, response):
                result = None
                break

        return result, response

    def isPaused(self):
        return self.paused

    def isRunning(self):
        return self.running

    def finishThreads(self):
        self.running = False
        self.finishedEvent.set()

    def isFinished(self):
        return self.runningThreadsCount == 0

    def stopThread(self):
        self.runningThreadsCount -= 1

    def reduceRate(self):
        self.rate -= 1

    def thread_proc(self):
        self.playEvent.wait()

        try:
            path = next(self.dictionary)

            while path:
                try:
                    # Pause if the request rate exceeded the maximum
                    while self.maxrate and self.rate > self.maxrate:
                        pass
                    self.rate += 1
                    threading.Timer(1, self.reduceRate).start()

                    status, response = self.scan(path)
                    result = Path(path=path, status=status, response=response)

                    if status:
                        self.matches.append(result)
                        for callback in self.matchCallbacks:
                            callback(result)
                    else:
                        for callback in self.notFoundCallbacks:
                            callback(result)

                except RequestException as e:
                    for callback in self.errorCallbacks:
                        callback(path, e.args[0]["message"])

                    continue

                finally:
                    if not self.playEvent.isSet():
                        self.stopped += 1
                        self.pausedSemaphore.release()
                        self.playEvent.wait()

                    path = next(self.dictionary)  # Raises StopIteration when finishes

                    if not self.running:
                        break

                    time.sleep(self.delay)

        except StopIteration:
            pass

        finally:
            self.stopThread()
