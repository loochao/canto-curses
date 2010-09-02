# -*- coding: utf-8 -*-
#Canto-curses - ncurses RSS reader
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

from command import CommandHandler, command_format
from taglist import TagList
from input import InputBox

from threading import Thread, Event, Lock
import logging
import curses
import time

log = logging.getLogger("SCREEN")

# The Screen class handles the layout of multiple sub-windows on the 
# main curses window. It's also the top-level gui object, so call to refresh the
# screen and get input should come through it.

# There are two types of windows that the Screen class handles. The first are
# normal windows (in self.windows). These windows are all tiled in a single
# layout (determined by self.layout and self.fill_layout()) and rendered first.

# The other types are floats that are rendered on top of the window layout.
# These floats are all independent of each other.

# The Screen class is also in charge of honoring the window specific
# configuration options. Like window.{maxwidth,maxheight,float}.

class Screen(CommandHandler):
    def __init__(self, user_queue, callbacks, types = [InputBox, TagList]):
        self.user_queue = user_queue
        self.callbacks = callbacks
        self.layout = "default"

        self.windows = [t() for t in types]
        self.floats = []

        self.keys = {}

        self.stdscr = curses.initscr()
        if self.curses_setup() < 0:
            return -1

        self.pseudo_input_box = curses.newpad(1,1)
        self.pseudo_input_box.keypad(1)
        self.pseudo_input_box.nodelay(1)
        self.input_lock = Lock()

        self.input_box = None
        self.sub_edit = False

        self.subwindows()

        # Start grabbing user input
        self.start_input_thread()

    # Do initial curses setup. This should only be done on init, or after
    # endwin() (i.e. resize).

    def curses_setup(self):
        # This can throw an exception, but we shouldn't care.
        try:
            # Turn off cursor.
            curses.curs_set(0)
        except:
            pass

        try:
            curses.cbreak()
            curses.noecho()
            curses.start_color()
            curses.use_default_colors()
        except Exception, e:
            log.error("Curses setup failed: %s" % e.msg)
            return -1

        self.height, self.width = self.stdscr.getmaxyx()

        for i, c in enumerate([ 7, 4, 3, 4, 2 ]):
            curses.init_pair(i + 1, c, -1)

        return 0

    # _subw_size functions enforce the height and width of windows.
    # It returns the minimum of:
    #       - The maximum size (given by layout)
    #       - The requested size (given by the class)
    #       - The configured size (given by the config)

    def _subw_size_height(self, ci, height):
        optname = ci.get_opt_name()
        cfg_height = self.callbacks["get_opt"](optname + ".maxheight")
        if not cfg_height:
            cfg_height = height
        req_height = ci.get_height(height)

        return min(height, cfg_height, req_height)

    def _subw_size_width(self, ci, width):
        optname = ci.get_opt_name()
        cfg_width = self.callbacks["get_opt"](optname + ".maxwidth")
        if not cfg_width:
            cfg_width = width
        req_width = ci.get_width(width)

        return min(width, cfg_width, req_width)

    # _subw_layout_size will return the total size of layout
    # in either height or width where layout is a list of curses
    # pads, or sublists of curses pads.

    def _subw_layout_size(self, layout, dim):

        # Grab index into pad.getmaxyx()
        if dim == "width":
            idx = 1
        elif dim == "height":
            idx = 0
        else:
            raise Exception("Unknown dim: %s" % dim)

        sizes = []
        for x in layout:
            if hasattr(x, "__iter__"):
                sizes.append(self._subw_layout_size(x, dim))
            else:
                sizes.append(x.pad.getmaxyx()[idx] - 1)

        return max(sizes)

    # Translate the layout into a set of curses pads given
    # a set of coordinates relating to how they're mapped to the screen.

    def _subw_init(self, ci, top, left, height, width):

        # Height - 1 because start + height = line after bottom.

        bottom = top + (height - 1)
        right = left + (width - 1)

        # lambda this up so that subwindows truly have no idea where on the
        # screen they are, only their dimensions, but can still selectively
        # refresh their portion of the screen.

        refcb = lambda : self.refresh_callback(ci, top, left, bottom, right)

        # Height + 1 to account for the last curses pad line
        # not being fully writable.

        pad = curses.newpad(height + 1, width)

        # Pass on callbacks we were given from CantoCursesGui
        # plus our own.

        callbacks = self.callbacks.copy()
        callbacks["refresh"] = refcb
        callbacks["input"] = self.input_callback
        callbacks["die"] = self.die_callback
        callbacks["pause_interface" ] = self.pause_interface_callback
        callbacks["unpause_interface"] = self.unpause_interface_callback
        callbacks["add_window"] = self.add_window_callback

        ci.init(pad, callbacks)

    # Layout some windows into the given space, stacking with
    # orientation horizontally or vertically.

    def _subw(self, layout, top, left, height, width, orientation):
        immediates = []
        cmplx = []
        sizes = [0] * len(layout)

        # Separate windows in to two categories:
        # immediates that are defined as base classes and
        # cmplx which are lists for further processing (iterables)

        for i, unit in enumerate(layout):
            if hasattr(unit, "__iter__"):
                cmplx.append((i, unit))
            else:
                immediates.append((i,unit))

        # Units are the number of windows we'll have
        # to split the area with.

        units = len(layout)

        # Used, the amounts of space already used.
        used = 0

        for i, unit in immediates:
            # Get the size of the window from the class.
            # Each class is given, as a maximum, the largest
            # possible slice we can *guarantee*.

            if orientation == "horizontal":
                size = self._subw_size_width(unit, (width - used) / units)
            else:
                size = self._subw_size_height(unit, (height - used) / units)

            used += size

            sizes[i] = size

            # Subtract so that the next run only divides
            # the remaining space by the number of units
            # that don't have space allocated.

            units -= 1

        # All of the immediates have been allocated for.
        # So now only the cmplxs are vying for space.

        units = len(cmplx)

        for i, unit in cmplx:
            offset = sum(sizes[0:i])

            # Recursives call this function, alternating
            # the orientation, for the space we can guarantee
            # this set of windows.

            if orientation == "horizontal":
                available = (width - used) / units
                r = self._subw(unit, top, left + offset,\
                        height, available, "vertical")
                sizes[i] = self._subw_layout_size(r, "width")
            else:
                available = (height - used) / units
                r = self._subw(unit, top + offset, left,\
                        available, width, "horizontal")
                sizes[i] = self._subw_layout_size(r, "height")

            used += sizes[i]
            units -= 1

        # Now that we know the actual sizes (and thus locations) of
        # the windows, we actually setup the immediates.

        for i, ci in immediates:
            offset = sum(sizes[0:i])
            if orientation == "horizontal":
                self._subw_init(ci, top, left + offset,
                        height, sizes[i])
            else:
                self._subw_init(ci, top + offset, left,
                        sizes[i], width)
        return layout

    # The fill_layout() function takes a list of active windows and generates a
    # list based layout. The depth of a window in the list determines its
    # orientation.
    #
    #   Example return: [ Window1, Window2 ]
    #       - Window1 on top of Window2, each taking half of the vertical space.
    #
    #   Example return: [ [ Window1, Window2 ], Window 3 ]
    #       - Window1 left of Window2 each taking half of the horizontal space,
    #           and whatever vertical space left by Window3, because Window3 is
    #           shallower than 1 or 2, so it's size is evaluated first and the
    #           remaining given to the [ Window1, Window2 ] horizontal layout.
    #
    #   Example return: [ [ [ [ Window1 ] ], Window2 ], Window3 ]
    #       - Same as above, except because Window1 is deeper than Window2 now,
    #           Window2's size is evaluated first and Window1 is given all of 
    #           the remaining space.
    #
    #   NOTE: Floating windows are not handled in the layout, this is solely for
    #   the tiling bottom layer of windows.

    def fill_layout(self, layout, windows):
        inputs = [ w for w in windows if w.is_input() ]
        if inputs:
            self.input_box = inputs[0]
        else:
            self.input_box = None

        # Simple stacking, even distribution between all windows.
        if layout == "hstack":
            return windows
        elif layout == "vstack":
            return [ windows ]
        else:
            aligns = { "top" : [], "bottom" : [], "left" : [], "right" : [],
                            "neutral" : [] }

            # Separate windows by alignment.
            for w in windows:
                align = self.callbacks["get_opt"](w.get_opt_name() + ".align")

                # Move taglist deeper so that it absorbs any
                # extra space left in the rest of the layout.

                if w.get_opt_name() == "taglist":
                    aligns[align].append([[w]])
                else:
                    aligns[align].append(w)

            horizontal = aligns["left"] + aligns["neutral"] + aligns["right"]
            return aligns["top"] + [horizontal] + aligns["bottom"]

    # subwindows() is the top level window generator. It handles both the bottom
    # level tiled window layout as well as the floats.

    def subwindows(self):
        # Focused window will no longer exist.
        self.focused = None

        # Generate tiled windows.
        l = self.fill_layout(self.layout, self.windows)
        self._subw(l, 0, 0, self.height, self.width, "vertical")

        # Generate floating windows.
        for f in self.floats: 
            align = self.callbacks["get_opt"](f.get_opt_name() + ".align")
            height = self._subw_size_height(f, self.height)
            width = self._subw_size_width(f, self.width)

            top = 0
            if align.startswith("bottom"):
                top = self.height - height

            left = 0
            if align.endswith("right"):
                left = self.width - width

            self._subw_init(f, top, left, height, width)

        # Default to giving first window focus.
        self._focus(0)

    def refresh_callback(self, c, t, l, b, r):
        if c in self.floats:
            b = min(b, t + c.pad.getyx()[0] + 1)
        c.pad.noutrefresh(0, 0, t, l, b, r)

    def input_callback(self, prompt):
        # Setup subedit
        self.input_done.clear()
        self.input_box.edit(prompt)
        self.sub_edit = True

        # Wait for finished input
        self.input_done.wait()

        # Grab the return and reset
        r = self.input_box.result
        self.input_box.reset()
        return r

    def die_callback(self, window):
        # Remove window from either window list or floating list.
        self.windows = [ w for w in self.windows if w != window ]
        self.floats = [ w for w in self.floats if w != window ]

        # Regenerate layout with remaining windows.
        self.subwindows()

        # If we lost focus, reset.
        if self.focused == window:
            self._focus(0)

        self.refresh()

    # The pause interface callback keeps the interface from updating. This is
    # useful if we have to temporarily surrender the screen (i.e. text browser).

    # NOTE: This does not affect signals so even while "paused", c-c continues
    # to take things like SIGWINCH which will be interpreted on wakeup.

    # NOTE: This callback must be called from within the GUI thread, and the
    # calling function must call unpause *without* returning.

    def pause_interface_callback(self):
        log.debug("Pausing interface.")
        self.input_lock.acquire()

    def unpause_interface_callback(self):
        log.debug("Unpausing interface.")
        self.input_lock.release()

        # All of our window information could be stale.
        self._resize()

    def add_window_callback(self, cls):
        ci = cls()

        # Enforce window.float
        optname = ci.get_opt_name()
        flt = self.callbacks["get_opt"](optname + ".float")
        if flt:
            self.floats.append(ci)
        else:
            self.windows.append(ci)

        self.subwindows()

        # Focus new window
        self._focus(0)

        self.refresh()

    # Optional integer return, if no arg, returns 0. (For focus)
    def optint(self, args):
        if not args:
            return (True, 0, "")
        t, r = self._first_term(args, None)
        try:
            t = int(t)
        except:
            log.error("Can't parse %s as integer" % t)
            return (False, None, None)
        return (True, t, r)

    # Refresh operates in order, which doesn't matter for top level tiled
    # windows, but this ensures that floats are ordered such that the last
    # floating window is rendered on top of all others.

    def refresh(self):
        for c in self.windows + self.floats:
            c.refresh()
        curses.doupdate()

    def redraw(self):
        for c in self.windows + self.floats:
            c.redraw()
        curses.doupdate()

    @command_format([])
    def cmd_resize(self, **kwargs):
        self._resize()

    # Typical curses resize, endwin and re-setup.
    def _resize(self):
        try:
            curses.endwin()
        except:
            pass

        self.pseudo_input_box.keypad(1)
        self.pseudo_input_box.nodelay(1)
        self.stdscr.refresh()

        self.curses_setup()
        self.subwindows()
        self.refresh()

    # Focus idx-th window.
    @command_format([("idx", "optint")])
    def cmd_focus(self, **kwargs):
        self._focus(kwargs["idx"])

    def _focus(self, idx):
        focus_order = self.windows + self.floats
        focus_order.reverse()
        l = len(focus_order)

        if -1 * l < idx < l:
            self.focused = focus_order[idx]
            log.debug("Focusing window %d (%s)" % (idx, self.focused))
        else:
            log.debug("Couldn't find window %d" % idx)

    # Pass a command to focused window:

    def command(self, cmd):
        if not CommandHandler.command(self, cmd) and self.focused:
            self.focused.command(cmd)

    def key(self, k):
        r = CommandHandler.key(self, k)
        if r:
            return r
        if self.focused:
            return self.focused.key(k)
        return None

    def input_thread(self):
        self.input_lock.acquire()
        while True:
            r = self.pseudo_input_box.getch()

            if r == -1:
                # Release the lock so that another thread can halt
                # this thread by holding this lock. (pause/unpause)
                self.input_lock.release()
                time.sleep(0.01)
                self.input_lock.acquire()
                continue

            log.debug("R = %s" % r)

            # We're in an edit box
            if self.sub_edit:
                # Feed the key to the input_box
                rc = self.input_box.addkey(r)

                # If rc == 1, need more keys
                # If rc == 0, all done (result could still be "" though)
                if not rc:
                    self.sub_edit = False
                    self.input_done.set()
                    self.callbacks["set_var"]("needs_redraw", True)
                continue

            # We're not in an edit box.

            # Convert to a writable character, if in the ASCII range
            if r < 256:
                r = chr(r)

            self.user_queue.put(("KEY", r))

    def start_input_thread(self):
        self.input_done = Event()
        self.inthread =\
                Thread(target = self.input_thread)

        self.inthread.daemon = True
        self.inthread.start()

    def exit(self):
        curses.endwin()
