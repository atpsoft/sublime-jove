# IMPLEMENT
# - C-x C-o
#
# - implement a build command which lets me specify the make command I want to run
# - implement forward/backward "defun" via scope selectors
# - implement forward/backward class definition via scope selectors
# - make fill paragraph smart about bulleted (i.e., ones that start with "-" or "*")
# - add support for "set mark automatically" commands
#   - move_to brackets but not necessarily other move_to's
#   - maybe get rid of your own move to eof and bof if you can get this working
#   - goto symbol stuff will be harder...
# - add an up-arrow (or meta-P meta-N) history mechanism for incremental search

# FIX
#
# fix comments so you can comment in the right column if no region is selected

import re, sys
import functools as fu
import sublime, sublime_plugin
from copy import copy

from .kill_ring import KillRing
from .mark_ring import MarkRing

JOVE_STATUS = "jove"

ISEARCH_ESCAPE_CMDS = ('move_to', 'jove_center_view', 'move', 'jove_universal_argument',
                       'jove_move_word', 'jove_move_to', 'scroll_lines')

default_jove_sexpr_separators = "./\\()\"'-:,.;<>~!@#$%^&*|+=[]{}`~?";
default_jove_word_separators = "./\\()\"'-_:,.;<>~!@#$%^&*|+=[]{}`~?";

# kill ring shared across all buffers
kill_ring = KillRing()

# ensure_visible commands
ensure_visible_cmds = set(['move', 'move_to'])

# initialized at the end of this file after all commands are defined
kill_cmds = set()

# repeatable commands
repeatable_cmds = set(['move', 'left_delete', 'right_delete'])

#
# We store state about each view.
#
class ViewState():
    # per view state
    view_state_dict = dict()

    # currently active view
    current = None

    # current in-progress i-search instance
    isearch_info = None

    def __init__(self, view):
        self.view = view
        self.active_mark = False

        # a mark ring per view (should be per buffer)
        self.mark_ring = MarkRing(view)
        self.reset()

    @classmethod
    def on_view_closed(cls, view):
        if view.id() in cls.view_state_dict:
            del(cls.view_state_dict[view.id()])

    @classmethod
    def get(cls, view):
        # make sure current is set to this view
        if ViewState.current is None or ViewState.current.view != view:
            state = cls.view_state_dict.get(view.id(), None)
            if state is None:
                state = ViewState(view)
                cls.view_state_dict[view.id()] = state
                state.view = view
            ViewState.current = state
        return ViewState.current

    def reset(self):
        self.this_cmd = None
        self.last_cmd = None
        self.argument_supplied = False
        self.argument_value = 0
        self.argument_negative = False
        self.drag_count = 0
        self.entered = 0

    #
    # Get the argument count and reset it for the next command (unless peek is True).
    #
    def get_count(self, peek=False):
        if self.argument_supplied:
            count = self.argument_value
            if self.argument_negative:
                if count == 0:
                    count = -1
                else:
                    count = -count
                if not peek:
                    self.argument_negative = False
            if not peek:
                self.argument_supplied = False
        else:
            count = 1
        return count

    def last_was_kill_cmd(self):
        return self.last_cmd in kill_cmds

class ViewWatcher(sublime_plugin.EventListener):
    def __init__(self, *args, **kwargs):
        super(ViewWatcher, self).__init__(*args, **kwargs)
        self.pending_dedups = 0

    def on_close(self, view):
        ViewState.on_view_closed(view)

    def on_modified(self, view):
        CmdHelper(view).toggle_active_mark_mode(False)

    def on_deactivated(self, view):
        info = ViewState.isearch_info
        if info and info.input_view == view:
            # deactivate immediately or else overlays will malfunction (we'll eat their keys)
            # we cannot dismiss the input panel because an overlay (if present) will lose focus
            info.deactivate()

    def on_activated_async(self, view):
        info = ViewState.isearch_info
        if info and not view.settings().get("is_widget"):
            # now we can dismiss the input panel
            info.done()

    def on_query_context(self, view, key, operator, operand, match_all):
        if key == "i_search_active":
            return ViewState.isearch_info and ViewState.isearch_info.is_active

    def on_post_save(self, view):
        # Schedule a dedup, but do not do it NOW because it seems to cause a crash if, say, we're
        # saving all the buffers right now. So we schedule it for the future.
        self.pending_dedups += 1
        def doit():
            self.pending_dedups -= 1
            if self.pending_dedups == 0:
                dedup_views(sublime.active_window())
        sublime.set_timeout(doit, 50)

class CmdWatcher(sublime_plugin.EventListener):
    def on_anything(self, view):
        view.erase_status(JOVE_STATUS)

    #
    # Override some commands to execute them N times if the numberic argument is supplied.
    #
    def on_text_command(self, view, cmd, args):
        if view.settings().get('is_widget') and ViewState.isearch_info:
            if cmd in ISEARCH_ESCAPE_CMDS:
                return ('jove_inc_search_escape', {'next_cmd': cmd, 'next_args': args})
            return

        vs = ViewState.get(view)
        self.on_anything(view)

        if args is None:
            args = {}

        # first keep track of this_cmd and last_cmd (if command starts with "jove_" it's handled
        # elsewhere)
        if not cmd.startswith("jove_"):
            vs.this_cmd = cmd

        #
        #  Process events that create a selection. The hard part is making it work with the emacs region.
        #
        if cmd == 'drag_select':
            if ViewState.isearch_info:
                ViewState.isearch_info.done()

            # Set drag_count to 0 when drag_select command occurs. BUT, if the 'by' parameter is
            # present, that means a double or triple click occurred. When that happens we have a
            # selection we want to start using, so we set drag_count to 2. 2 is the number of
            # drag_counts we need in the normal course of events before we turn on the active mark
            # mode.
            vs.drag_count = 2 if 'by' in args else 0

        if cmd in ('move', 'move_to') and vs.active_mark and not args.get('extend', False):
            args['extend'] = True
            return (cmd, args)

        # now check for numeric argument and rewrite some commands as necessary
        if not vs.argument_supplied:
            return

        if cmd in repeatable_cmds:
            count = vs.get_count()
            args.update({
                'cmd': cmd,
                '_times': abs(count),
            })
            if count < 0 and 'forward' in args:
                args['forward'] = not args['forward']
            return ("jove_do_times", args)
        elif cmd == 'scroll_lines':
            args['amount'] *= vs.get_count()
            return (cmd, args)

    #
    # Post command processing: deal with active mark and resetting the numeric argument.
    #
    def on_post_text_command(self, view, cmd, args):
        vs = ViewState.get(view)
        cm = CmdHelper(view)
        if vs.active_mark and vs.this_cmd != 'drag_select' and vs.last_cmd == 'drag_select':
            # if we just finished a mouse drag, make sure active mark mode is off
            cm.toggle_active_mark_mode(False)

        # reset numeric argument (if command starts with "jove_" this is handled elsewhere)
        if not cmd.startswith("jove_"):
            vs.argument_value = 0
            vs.argument_supplied = False
            vs.last_cmd = cmd

        if vs.active_mark:
            if len(view.sel()) > 1:
                # allow the awesomeness of multiple cursors to be used: the selection will disappear
                # after the next command
                vs.active_mark = False
            else:
                cm.set_selection(cm.get_mark(), cm.get_point())

        if cmd in ensure_visible_cmds and cm.just_one_point():
            cm.ensure_visible(cm.get_point())

    #
    # Process the selection if it was created from a drag_select (mouse dragging) command.
    #
    def on_selection_modified(self, view):
        vs = ViewState.get(view)
        selection = view.sel()

        if len(selection) == 1 and vs.this_cmd == 'drag_select':
            cm = CmdHelper(view, vs);
            if vs.drag_count == 2:
                # second event - enable active mark
                region = view.sel()[0]
                mark = region.a
                cm.set_mark(mark, and_selection=False)
                cm.toggle_active_mark_mode(True)
            elif vs.drag_count == 0:
                cm.toggle_active_mark_mode(False)
        vs.drag_count += 1


    #
    # At a minimum this is called when bytes are inserted into the buffer.
    #
    def on_modified(self, view):
        ViewState.get(view).this_cmd = None
        self.on_anything(view)


#
# A helper class which provides a bunch of useful functionality on a view
#
class CmdHelper:
    def __init__(self, view, state=None, edit=None):
        self.view = view
        if state is None:
            state = ViewState.get(self.view)
        self.state = state
        self.edit = edit

    #
    # Sets the status text on the bottom of the window.
    #
    def set_status(self, msg):
        self.view.set_status(JOVE_STATUS, msg)

    #
    # Returns point. Point is where the cursor is in the possibly extended region. If there are multiple cursors it
    # uses the first one in the list.
    #
    def get_point(self):
        sel = self.view.sel()
        if len(sel) > 0:
            return sel[0].b
        return -1

    #
    # This no-op ensures the next/prev line target column is reset to the new locations.
    #
    def reset_target_column(self):
        selection = self.view.sel()
        if len(selection) == 1 and selection[0].empty() and selection[0].b < self.view.size():
            self.run_command("move", {"by": "characters", "forward": True})
            self.run_command("move", {"by": "characters", "forward": False})

    #
    # Returns the mark position.
    #
    def get_mark(self):
        mark = self.view.get_regions("jove_mark")
        if mark:
            mark = mark[0]
            return mark.a

    #
    # Get the region between mark and point.
    #
    def get_region(self):
        selection = self.view.sel()
        if len(selection) != 1:
            # Oops - this error message does not belong here!
            self.set_status("Operation not supported with multiple cursors")
            return
        selection = selection[0]
        if selection.size() > 0:
            return selection
        mark = self.get_mark()
        if mark is not None:
            point = self.get_point()
            return sublime.Region(mark, self.get_point())

    #
    # Save a copy of the current region in the named mark. This mark will be robust in the face of
    # changes to the buffer.
    #
    def save_region(self, name):
        r = self.get_region()
        if r:
            self.view.add_regions(name, [r], "mark", "", sublime.HIDDEN)
        return r

    #
    # Restore the current region to the named saved mark.
    #
    def restore_region(self, name):
        r = self.view.get_regions(name)
        if r:
            r = r[0]
            self.set_mark(r.a, False, False)
            self.set_selection(r.b, r.b)
            self.view.erase_regions(name)
        return r

    #
    # Iterator on all the lines in the specified sublime Region.
    #
    def for_each_line(self, region):
        view = self.view
        pos = region.begin()
        limit = region.end()
        while pos < limit:
            line = view.line(pos)
            yield line
            pos = line.end() + 1

    #
    # Returns true if all the text between a and b is blank.
    #
    def is_blank(self, a, b):
        text = self.view.substr(sublime.Region(a, b))
        return re.match(r'[ \t]*$', text) is not None

    #
    # Returns the current indent of the line containing the specified POS and the column of POS.
    #
    def get_line_indent(self, pos):
        data,col,region = self.get_line_info(pos)
        m = re.match(r'[ \t]*', data)
        return (len(m.group(0)), col)

    #
    # Sets the buffers mark to the specified pos (or the current position in the view).
    #
    def set_mark(self, pos=None, update_status=True, and_selection=True):
        view = self.view
        mark_ring = self.state.mark_ring

        if pos is None:
            pos = self.get_point()

        # update the mark ring
        mark_ring.set(pos)

        if and_selection:
            self.set_selection(pos, pos)
        if update_status:
            self.set_status("Mark Saved")

    #
    # Enabling active mark means highlight the current emacs region.
    #
    def toggle_active_mark_mode(self, value=None):
        if value is not None and self.state.active_mark == value:
            return

        self.state.active_mark = value if value is not None else (not self.state.active_mark)
        point = self.get_point()
        if self.state.active_mark:
            mark = self.get_mark()
            self.set_selection(mark, point)
            self.state.active_mark = True
        else:
            self.set_selection(point, point)

    def swap_point_and_mark(self):
        view = self.view
        mark_ring = self.state.mark_ring
        mark = mark_ring.exchange(self.get_point())
        if mark is not None:
            self.goto_position(mark)
        else:
            self.set_status("No mark in this buffer")

    def set_selection(self, a=None, b=None):
        if a is None:
            a = b = self.get_point()
        selection = self.view.sel()
        selection.clear()
        selection.add(sublime.Region(a, b))

    def get_line_info(self, point):
        view = self.view
        region = view.line(point)
        data = view.substr(region)
        row,col = view.rowcol(point)
        return (data, col, region)

    def run_window_command(self, cmd, args):
        self.view.window().run_command(cmd, args)

    def has_prefix_arg(self):
        return self.state.argument_supplied

    def just_one_point(self):
        return len(self.view.sel()) == 1

    def get_count(self, peek=False):
        return self.state.get_count(peek)

    #
    # This provides a way to run a function on all the cursors, one after another. This maintains
    # all the cursors and then calls the function with one cursor at a time, with the view's
    # selection state set to just that one cursor. So any calls to run_command within the function
    # will operate on only that one cursor.
    #
    # The called function is supposed to return a new cursor position or None, in which case value
    # is taken from the view itself.
    #
    # REMIND: This isn't how it currently works!
    #
    # After the function is run on all the cursors, the view's multi-cursor state is restored with
    # new values for the cursor.
    #
    def for_each_cursor(self, function, *args, **kwargs):
        view = self.view
        selection = view.sel()

        # copy cursors into proper regions which sublime will manage while we potentially edit the
        # buffer and cause things to move around
        key = "tmp_cursors"
        cursors = [c for c in selection]
        view.add_regions(key, cursors, "tmp", "", sublime.HIDDEN)

        # run the command passing in each cursor and collecting the returned cursor
        for i in range(len(cursors)):
            selection.clear()
            regions = view.get_regions(key)
            if i >= len(regions):
                # we've deleted some cursors along the way - we're done
                break
            cursor = regions[i]
            selection.add(cursor)
            cursor = function(cursor, *args, **kwargs)
            if cursor is not None:
                # update the cursor in its slot
                regions[i] = cursor
                view.add_regions(key, regions, "tmp", "", sublime.HIDDEN)

        # restore the cursors
        selection.clear()
        selection.add_all(view.get_regions(key))
        view.erase_regions(key)

    def goto_line(self, line):
        if line >= 0:
            view = self.view
            point = view.text_point(line - 1, 0)
            self.goto_position(point, set_mark=True)

    def goto_position(self, pos, set_mark=False):
        if set_mark and self.get_point() != pos:
            self.set_mark()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(pos, pos))
        self.ensure_visible(pos)

    def is_visible(self, pos):
        visible = self.view.visible_region()
        return visible.contains(pos)

    def ensure_visible(self, point, force=False):
        if force or not self.is_visible(point):
            self.view.show_at_center(point)

    def is_word_char(self, pos, forward, separators):
        if not forward:
            if pos == 0:
                return False
            pos -= 1
        char = self.view.substr(pos)
        return not (char in " \t\r\n" or char in separators)

    #
    # Goes to the other end of the scope at the specified position. The specified position should be
    # around brackets or quotes.
    #
    def to_other_end(self, point, direction):
        brac = "([{"
        kets = ")]}"

        view = self.view
        scope_name = view.scope_name(point)
        if scope_name.find("comment") >= 0:
            return None

        ch = view.substr(point)
        if direction > 0 and view.substr(point) in brac:
            return self.run_command("move_to", {"to": "brackets"}, point=point)
        elif direction < 0 and view.substr(point - 1) in kets:
            # this can be tricky due to inconsistencies with sublime bracket matching
            # we need to handle "))" and "()[0]" when between the ) and [
            if point < view.size() and view.substr(point) in brac:
                # go inside the bracket (point - 1), then to the inside of the match, then back one more
                return self.run_command("move_to", {"to": "brackets"}, point=point - 1) - 1
            else:
                return self.run_command("move_to", {"to": "brackets"}, point=point)

        # otherwise it's a string
        start = point + direction
        self.run_command("expand_selection", {"to": "scope"}, point=start)
        r = view.sel()[0]
        return r.end() if direction > 0 else r.begin()

    #
    # Run the specified command and args in the current view. If point is specified set point in the
    # view before running the command. Returns the resulting point.
    #
    def run_command(self, cmd, args, point=None):
        view = self.view
        if point is not None:
            view.sel().clear()
            view.sel().add(sublime.Region(point, point))
        view.run_command(cmd, args)
        return self.get_point()

#
# The baseclass for JOVE commands. This sets up state, creates a helper, processes the universal
# argument, and then calls the run_cmd method, which subclasses should override.
#
class JoveTextCommand(sublime_plugin.TextCommand):
    should_reset_target_column = False
    is_kill_cmd = False
    is_ensure_visible_cmd = False
    unregistered = False

    def run(self, edit, **kwargs):
        # get our view state
        vs = ViewState.get(self.view)

        # first keep track of this_cmd and last_cmd but only if we're not called recursively
        cmd = self.jove_cmd_name

        if vs.entered == 0 and (cmd != 'jove_universal_argument' or self.unregistered):
            vs.this_cmd = cmd
        vs.entered += 1
        jove = CmdHelper(self.view, state=vs, edit=edit)
        try:
            self.run_cmd(jove, **kwargs)
        finally:
            vs.entered -= 1
        if vs.entered == 0 and (cmd != 'jove_universal_argument' or self.unregistered):
            vs.last_cmd = vs.this_cmd
            vs.argument_value = 0
            vs.argument_supplied = False

            # this no-op ensures the next/prev line target column is reset to the new locations
            if self.should_reset_target_column:
                jove.reset_target_column()

#
# Calls run command a specified number of times.
#
class JoveDoTimesCommand(JoveTextCommand):
    def run_cmd(self, jove, cmd, _times, **args):
        view = self.view
        visible = view.visible_region()
        for i in range(_times):
            view.run_command(cmd, args)
        point = jove.get_point()
        if not visible.contains(point):
            jove.ensure_visible(point, True)

class JoveShowScopeCommand(JoveTextCommand):
    def run_cmd(self, jove, direction=1):
        point = jove.get_point()
        name = self.view.scope_name(point)
        region = self.view.extract_scope(point)
        status = "%d bytes: %s" % (region.size(), name)
        print(status)
        self.view.set_status(JOVE_STATUS, status)

class JoveMoveWordCommand(JoveTextCommand):
    should_reset_target_column = True
    is_ensure_visible_cmd = True

    def run_cmd(self, jove, direction=1):
        view = self.view

        settings = view.settings()
        separators = settings.get("jove_word_separators", default_jove_word_separators)

        # determine the direction
        count = jove.get_count() * direction
        forward = count > 0
        count = abs(count)

        def move_word0(cursor, first=False, **kwargs):
            point = cursor.b
            if forward:
                if not first or not jove.is_word_char(point, True, separators):
                    point = view.find_by_class(point, True, sublime.CLASS_WORD_START, separators)
                point = view.find_by_class(point, True, sublime.CLASS_WORD_END, separators)
            else:
                if not first or not jove.is_word_char(point, False, separators):
                    point = view.find_by_class(point, False, sublime.CLASS_WORD_END, separators)
                point = view.find_by_class(point, False, sublime.CLASS_WORD_START, separators)
            cursor.a = cursor.b = point
            return cursor

        for c in range(count):
            jove.for_each_cursor(move_word0, first=(c == 0))

#
# Advance to the beginning (or end if going backward) word unless already positioned at a word
# character. This can be used as setup for commands like upper/lower/capitalize words. This ignores
# the argument count.
#
class JoveToWordCommand(JoveTextCommand):
    should_reset_target_column = True

    def run_cmd(self, jove, direction=1):
        view = self.view

        settings = view.settings()
        separators = settings.get("jove_word_separators", default_jove_word_separators)
        forward = direction > 0

        def to_word(cursor):
            point = cursor.b
            if forward:
                if not jove.is_word_char(point, True, separators):
                    point = view.find_by_class(point, True, sublime.CLASS_WORD_START, separators)
            else:
                if not jove.is_word_char(point, False, separators):
                    point = view.find_by_class(point, False, sublime.CLASS_WORD_END, separators)
            cursor.a = cursor.b = point
            return cursor

        jove.for_each_cursor(to_word)

class JoveCaseWordCommand(JoveTextCommand):
    should_reset_target_column = True

    def run_cmd(self, jove, mode, direction=1):
        count = jove.get_count() * direction
        direction = -1 if count < 0 else 1
        count = abs(count)
        args = {"direction": direction}

        def case_word(cursor):
            if direction < 0:
                # modify the N words before point by going back N words first
                orig_point = cursor.a
                saved = jove.save_region("tmp")
                for i in range(count):
                    jove.run_command("jove_move_word", args)
                args['direction'] = -args['direction']

            for i in range(count):
                # go to beginning of word (or stay where we are)
                jove.run_command('jove_to_word', args)
                cursor.a = jove.get_point()

                # stretch the selection to the end of the word, making sure we don't zip past our
                # start if we were going backwards
                jove.run_command('jove_move_word', args)
                cursor.b = jove.get_point()
                if direction < 0 and cursor.b > orig_point:
                    cursor.b = orig_point

                # now convert the text in the selection
                old_text = text = jove.view.substr(cursor)
                if mode == "title":
                    text = text.title()
                elif mode == 'lower':
                    text = text.lower()
                elif mode == 'upper':
                    text= text.upper()
                else:
                    print("Unknown mode", mode)
                if old_text != text:
                    jove.view.erase(jove.edit, cursor)
                    jove.view.insert(jove.edit, cursor.a, text)

            if direction < 0:
                cursor.a = cursor.b = orig_point
            else:
                cursor.a = cursor.b = jove.get_point()
            return cursor
        jove.for_each_cursor(case_word)

class JoveMoveSexprCommand(JoveTextCommand):
    is_ensure_visible_cmd = True
    should_reset_target_column = True

    def run_cmd(self, jove, direction=1):
        view = self.view

        settings = view.settings()
        separators = settings.get("jove_sexpr_separators", default_jove_sexpr_separators)

        # determine the direction
        count = jove.get_count() * direction
        forward = count > 0
        count = abs(count)

        def advance(cursor, first=False, **kwargs):
            point = cursor.b
            if forward:
                limit = view.size()
                while point < limit:
                    if jove.is_word_char(point, True, separators):
                        point = view.find_by_class(point, True, sublime.CLASS_WORD_END, separators)
                        break
                    else:
                        ch = view.substr(point)
                        if ch in "({['\"":
                            next_point = jove.to_other_end(point, direction)
                            if next_point is not None:
                                point = next_point
                                break
                        point += 1
            else:
                while point > 0:
                    if jove.is_word_char(point, False, separators):
                        point = view.find_by_class(point, False, sublime.CLASS_WORD_START, separators)
                        break
                    else:
                        ch = view.substr(point - 1)
                        if ch in ")}]'\"":
                            next_point = jove.to_other_end(point, direction)
                            if next_point is not None:
                                point = next_point
                                break
                        point -= 1

            cursor.a = cursor.b = point
            return cursor

        for c in range(count):
            jove.for_each_cursor(advance, first=(c == 0))

#
# This command remembers all the current cursor positions, executes a command on all the cursors,
# and then deletes all the data between the two.
#
# If there's only one selection, the deleted data is added to the kill ring appropriately.
#
class JoveMoveThenDeleteCommand(JoveTextCommand):
    is_ensure_visible_cmd = True
    is_kill_cmd = True

    def run_cmd(self, jove, move_cmd, **kwargs):
        view = self.view
        selection = view.sel()

        # peek at the count
        count = jove.get_count(True)
        if 'direction' in kwargs:
            count *= kwargs['direction']

        # remember the current cursor positions
        orig_cursors = [s for s in selection]
        view.run_command(move_cmd, kwargs)

        # extend each cursor so we can delete the bytes, and only if there is only one region will
        # we add the data to the kill ring
        new_cursors = [s for s in selection]

        selection.clear()
        for old,new in zip(orig_cursors, new_cursors):
            if old < new:
                selection.add(sublime.Region(old.begin(), new.end()))
            else:
                selection.add(sublime.Region(new.begin(), old.end()))

        # only append to kill ring if there's one selection
        if len(selection) == 1:
            kill_ring.add(view.substr(selection[0]), forward=count > 0, join=jove.state.last_was_kill_cmd())

        for region in selection:
            view.erase(jove.edit, region)

class JoveGotoLineCommand(JoveTextCommand):
    def run_cmd(self, jove):
        if jove.has_prefix_arg():
            jove.goto_line(jove.get_count())
        else:
            self.run_window_command("show_overlay", {"overlay": "goto", "text": ":"})

class JoveDeleteWhiteSpaceCommand(JoveTextCommand):
    def run_cmd(self, jove):
        jove.for_each_cursor(self.delete_white_space, jove)

    def delete_white_space(self, cursor, jove, **kwargs):
        view = self.view
        line = view.line(cursor.a)
        data = view.substr(line)
        row,col = view.rowcol(cursor.a)
        start = col
        while start - 1 >= 0 and data[start-1: start] in (" \t"):
            start -= 1
        end = col
        limit = len(data)
        while end + 1 < limit and data[end:end+1] in (" \t"):
            end += 1
        view.erase(jove.edit, sublime.Region(line.begin() + start, line.begin() + end))
        return None

class JoveUniversalArgumentCommand(JoveTextCommand):
    def run_cmd(self, jove, value):
        state = jove.state
        if not state.argument_supplied:
            state.argument_supplied = True
            if value == 'by_four':
                state.argument_value = 4
            elif value == 'negative':
                state.argument_negative = True
            else:
                state.argument_value = value
        elif value == 'by_four':
            state.argument_value *= 4
        elif isinstance(value, int):
            state.argument_value *= 10
            state.argument_value += value
        elif value == 'negative':
            state.argument_value = -state.argument_value

class JoveShiftRegionCommand(JoveTextCommand):
    """Shifts the emacs region left or right."""

    def run_cmd(self, jove, direction):
        view = self.view
        state = jove.state
        r = jove.save_region("shift")
        if r:
            jove.toggle_active_mark_mode(False)
            selection = self.view.sel()
            selection.clear()

            # figure out how far we're moving
            if state.argument_supplied:
                cols = direction * jove.get_count()
            else:
                cols = direction * self.view.settings().get("tab_size")

            # now we know which way and how far we're shifting, create a cursor for each line we
            # want to shift
            amount = abs(cols)
            count = 0
            shifted = 0
            for line in jove.for_each_line(r):
                count += 1
                if cols < 0 and (line.size() < amount or not jove.is_blank(line.a, line.a + amount)):
                    continue
                selection.add(sublime.Region(line.a, line.a))
                shifted += 1

            # shift the region
            if cols > 0:
                # shift right
                self.view.run_command("insert", {"characters": " " * cols})
            else:
                for i in range(amount):
                    self.view.run_command("right_delete")

            # restore the region
            jove.restore_region("shift")
            sublime.set_timeout(lambda: jove.set_status("Shifted %d of %d lines in the region" % (shifted, count)), 100)

class JoveCenterViewCommand(JoveTextCommand):
    def run_cmd(self, jove):
        view = self.view
        point = jove.get_point()
        if jove.has_prefix_arg():
            lines = jove.get_count()
            line_height = view.line_height()
            ignore, point_offy = view.text_to_layout(point)
            offx, ignore = view.viewport_position()
            view.set_viewport_position((offx, point_offy - line_height * lines))
        else:
            view.show_at_center(point)

class JoveSetMarkCommand(JoveTextCommand):
    def run_cmd(self, jove):
        state = jove.state
        if state.argument_supplied:
            pos = state.mark_ring.pop()
            if pos:
                jove.goto_position(pos)
            else:
                jove.set_status("No mark to pop!")
            state.this_cmd = "jove_pop_mark"
        elif state.this_cmd == state.last_cmd:
            # at least two set mark commands in a row: turn ON the highlight
            jove.toggle_active_mark_mode()
        else:
            # set the mark
            state.active_mark = False
            jove.set_mark()

class JoveSwapPointAndMarkCommand(JoveTextCommand):
    def run_cmd(self, jove):
        if jove.state.argument_supplied:
            jove.toggle_active_mark_mode()
        else:
            jove.swap_point_and_mark()

class JoveMoveToCommand(JoveTextCommand):
    is_ensure_visible_cmd = True
    def run_cmd(self, jove, to):
        if to == 'bof':
            jove.goto_position(0, set_mark=True)
        elif to == 'eof':
            jove.goto_position(self.view.size(), set_mark=True)
        elif to in ('eow', 'bow'):
            visible = self.view.visible_region()
            jove.goto_position(visible.a if to == 'bow' else visible.b)

class JoveOpenLineCommand(JoveTextCommand):
    def run_cmd(self, jove):
        view = self.view
        for point in view.sel():
            view.insert(jove.edit, point.b, "\n")
        view.run_command("move", {"by": "characters", "forward": False})

class JoveKillRegionCommand(JoveTextCommand):
    is_kill_cmd = True
    def run_cmd(self, jove, is_copy=False):
        view = self.view
        region = jove.get_region()
        if region:
            bytes = region.size()
            kill_ring.add(view.substr(region), True, False)
            if not is_copy:
                view.erase(jove.edit, region)
            else:
                jove.set_status("Copied %d bytes" % (bytes,))
            jove.toggle_active_mark_mode(False)

class JovePaneCmdCommand(JoveTextCommand):
    def run_cmd(self, jove, cmd, **kwargs):
        view = self.view
        window = view.window()
        if cmd == 'split':
            self.split(window, jove, **kwargs)
        elif cmd == 'grow':
            self.grow(window, jove, **kwargs)
        elif cmd == 'destroy':
            self.destroy(window, jove, **kwargs)
        elif cmd == 'move':
            self.move(window, jove, **kwargs)
        else:
            print("Unknown command")

    #
    # Grow the current selected window group (pane). Amount is usually 1 or -1 for grow and shrink.
    #
    def grow(self, window, jove, amount):
        def rows_to_sizes(rows):
            sizes = []
            for i in range(len(rows) - 1):
                sizes.append(rows[i + 1] - rows[i])
            return sizes

        def sizes_to_rows(sizes):
            rows = [0]
            off = 0
            for s in sizes:
                off += s
                rows.append(off)
            return rows

        if window.num_groups() == 1:
            return
        layout = window.layout()
        sizes = rows_to_sizes(layout['rows'])
        current = window.active_group()
        view = jove.view

        line_height = view.line_height()

        # calulcate the percentage for a single line: view_percentage * line_height/view_height
        one_line = sizes[current] * (line_height / view.viewport_extent()[1])
        amnt = one_line * jove.get_count() * amount

        other = current - 1 if current == window.num_groups() - 1 else current + 1
        total = sizes[other] + sizes[current]
        sizes[other] -= amnt
        sizes[current] += amnt
        if sizes[other] < .1:
            sizes[other] = .1
            sizes[current] = total - .1

        layout['rows'] = sizes_to_rows(sizes)
        window.set_layout(layout)
        window.active_view_in_group(other).show(jove.get_point())

    #
    # Split the current pane in half. Clone the current view into the new pane. Refuses to split if
    # the resulting windows would be too small.
    #
    def split(self, window, jove):
        # For reference: this is what a layout looks like
        # {'cells': [[0, 0, 1, 1], [0, 1, 1, 2], [0, 2, 1, 3]], 'cols': [0.0, 1.0], 'rows': [0.0, 0.3097919170673077, 0.6548959585336538, 1.0]}
        view = jove.view
        layout = window.layout()
        rows = layout['rows']

        # this is the one we're splitting
        current = window.active_group()
        y0 = rows[current]
        y1 = rows[current + 1]

        # we're willing to split down to 4 lines in each window
        if view.viewport_extent()[1] / 2 <= 4 * view.line_height():
            return

        rows.insert(current + 1, (y1 + y0) / 2)

        # generate new cells
        layout['cells'] = [[0, index, 1, index + 1] for index in range(len(rows) - 1)]
        window.set_layout(layout)

        # look for another view in the current group that is already displaying this file
        new_view = None
        for v in window.views_in_group(current):
            if v != view and v.buffer_id() == view.buffer_id():
                new_view = v
                break
        if new_view is None:
            # couldn't find an existing view so we have to clone the current one
            window.run_command("clone_file")

            # the cloned view becomes the new active view
            new_view = window.active_view()

        # move the new view into the new group (current + 1)
        window.set_view_index(new_view, current + 1, 0)

        # make sure the original view is the focus in the original pane
        window.focus_view(view)

        # switch to new pane
        window.focus_group(current + 1)

        # after a short delay make sure the two views are looking at the same area
        def setup_views():
            selection = new_view.sel()
            selection.clear()
            selection.add_all([r for r in view.sel()])
            new_view.set_viewport_position(view.viewport_position(), False)

            point = jove.get_point()
            new_view.show(point)
            view.show(point)

        sublime.set_timeout(setup_views, 10)

    #
    # Destroy the specified pane=self|others.
    #
    def destroy(self, window, jove, pane):
        if window.num_groups() == 1:
            return
        view = jove.view
        layout = window.layout()
        rows = layout['rows']
        current = window.active_group()

        # remember which views are where
        views = [window.active_view_in_group(i) for i in range(window.num_groups())]

        if pane == "self":
            del(views[current])

            # we give the space to the window above if possible
            del(rows[max(1, current)])
        else:
            # delete all but the first and last elements
            rows[1:-1] = []
            views = [jove.view]

        # generate new cells
        layout['cells'] = [[0, index, 1, index + 1] for index in range(len(rows) - 1)]
        window.set_layout(layout)
        for i in range(window.num_groups()):
            view = views[i]
            window.focus_group(i)
            window.focus_view(view)

        window.focus_group(max(0, current - 1))
        dedup_views(window)

    def move(self, window, jove, direction):
        if direction in ("prev", "next"):
            direction = 1 if direction == "next" else -1
            current = window.active_group()
            current += direction
            num_groups = window.num_groups()
            if current < 0:
                current = num_groups - 1
            elif current >= num_groups:
                current = 0
            window.focus_group(current)
        else:
            group,index = window.get_view_index(jove.view)
            views = window.views_in_group(group)
            direction = 1 if direction == "right" else -1
            index += direction
            if index >= len(views):
                index = 0
            elif index < 0:
                index = len(views) - 1
            window.focus_view(views[index])

#
# Exists only to support kill-line with multiple cursors.
#
class JoveMoveForKillLineCommand(JoveTextCommand):
    def run_cmd(self, jove, **kwargs):
        view = self.view
        state = jove.state

        if state.argument_supplied:
            # we don't support negative arguments for kill-line
            count = abs(jove.get_count())
            line_mode = True
        else:
            line_mode = False

        def advance(cursor):
            start = cursor.b
            text,index,region = jove.get_line_info(start)

            if line_mode:
                # go down N lines
                for i in range(abs(count)):
                    view.run_command("move", {"by": "lines", "forward": True})

                end = jove.get_point()
                if region.contains(end):
                    # same line we started on - must be on the last line of the file
                    end = region.end()
                else:
                    # beginning of the line we ended up on
                    end = view.line(jove.get_point()).begin()
                    jove.goto_position(end, set_mark=False)
            else:
                end = region.end()

                # check if line is blank from here to the end and if so, delete the \n as well
                import re
                if re.match(r'[ \t]*$', text[index:]):
                    end += 1
            cursor.a = cursor.b = end
            return cursor

        jove.for_each_cursor(advance)

class JoveYankCommand(JoveTextCommand):
    def run_cmd(self, jove, pop=0):
        # for now only works with one cursor
        view = self.view
        selection = view.sel()
        if len(selection) != 1:
            jove.set_status("Cannot yank with multiple cursors ... yet")
            return

        if pop != 0:
            # we need to delete the existing data first
            if jove.state.last_cmd != 'jove_yank':
                jove.set_status("Previous command was not yank!")
                return
            view.erase(jove.edit, jove.get_region())

        data = kill_ring.get_current(pop)
        if data:
            point = jove.get_point()
            view.insert(jove.edit, point, data)
            jove.state.mark_ring.set(point, True)
            jove.ensure_visible(jove.get_point())
        else:
            jove.set_status("Nothing to pop!")

#####################################################
#            Better incremental search              #
#####################################################
class ISearchInfo():
    last_search = None

    class StackItem():
        def __init__(self, search, regions, selected, current_index, forward, wrapped):
            self.prev = None
            self.search = search
            self.regions = regions
            self.selected = selected
            self.current_index = current_index
            self.forward = forward
            self.try_wrapped = False
            self.wrapped = wrapped
            if current_index >= 0 and regions:
                # add the new one to selected
                selected.append(regions[current_index])

        def get_point(self):
            if self.current_index >= 0:
                r = self.regions[self.current_index]
                return r.begin() if self.forward else r.end()
            return None

        def clone(self):
            return copy.copy(self)

        #
        # Clone is called when we want to make progress with the same search string as before.
        #
        def step(self, forward, keep):
            index = self.current_index
            matches = len(self.regions)
            if (self.regions and (index < 0 or (index == 0 and not forward) or (index == matches - 1) and forward)):
                # wrap around!
                index = 0 if forward else matches - 1
                if self.try_wrapped or not self.regions:
                    wrapped = True
                    self.try_wrapped = False
                else:
                    self.try_wrapped = True
                    return None
            elif (forward and index < matches - 1) or (not forward and index > 0):
                index = index + 1 if forward else index - 1
                wrapped = self.wrapped
            else:
                return None
            selected = copy(self.selected)
            if not keep and len(selected) > 0:
                del(selected[-1])
            return ISearchInfo.StackItem(self.search, self.regions, selected, index, forward, wrapped)


    def __init__(self, view, forward, regex):
        self.view = view
        self.current = ISearchInfo.StackItem("", [], [], -1, forward, False)
        self.jove = CmdHelper(view)
        self.window = view.window()
        self.point = self.jove.get_point()
        self.update()
        self.input_view = None
        self.in_changes = 0
        self.forward = forward
        self.is_active = True
        self.regex = regex

    def open(self):
        window = self.view.window()
        self.input_view = window.show_input_panel("%sI-Search:" % ("Regexp " if self.regex else "", ),
                                                  "", self.on_done, self.on_change, self.on_cancel)

    def is_active(self):
        return ViewState.isearch_info == self

    def on_done(self, val):
        # on_done: stop the search, keep the cursors intact
        ViewState.isearch_info = None
        if self.is_active:
            self.finish(abort=False)

    def on_cancel(self):
        # on_cancel: stop the search, go back to start
        ViewState.isearch_info = None
        if self.is_active:
            self.finish(abort=True)

    def on_change(self, val):
        if self.in_changes > 0:
            # When we pop back to an old state, we have to replace the search string with what was
            # in effect at that state. We do that by deleting all the text and inserting the value
            # of the search string. This causes this on_change method to be called. We want to
            # ignore it, which is what we're doing here.
            self.in_changes -= 1
            return

        self.find(val)

    def find(self, val):
        # determine if this is case sensitive search or not
        flags = 0 if self.regex else sublime.LITERAL
        if not re.search(r'[A-Z]', val):
            flags |= sublime.IGNORECASE

        # find all instances if we have a search string
        if len(val) > 0:
            regions = self.view.find_all(val, flags)

            # find the closest match to where we currently are
            point = None
            if self.current:
                point = self.current.get_point()
            if point is None:
                point = self.point
            index = self.find_closest(regions, point, self.forward)

            # push this new state onto the stack
            self.push(ISearchInfo.StackItem(val, regions, [], index, self.forward, self.current.wrapped))
        else:
            regions = None
            index = -1
        self.update()

    #
    # Implementation and internal API.
    #

    #
    # Push a new state onto the stack.
    #
    def push(self, item):
        item.prev = self.current
        self.current = item

    #
    # Pop one state of the stack and restore everything to the state at that time.
    #
    def pop(self):
        if self.current.prev:
            self.current = self.current.prev
            self.set_text(self.current.search)
            self.forward = self.current.forward
            self.update()
        else:
            print("Nothing to pop so not updating!")

    def deactivate(self):
        self.is_active = False
        self.finish(abort=False)

    def done(self):
        # close the panel which should trigger an on_done
        self.view.window().run_command("hide_panel")

    #
    # Set the text of the search to a particular value. If is_pop is True it means we're restoring
    # to a previous state. Otherwise, we want to pretend as though this text were actually inserted.
    #
    def set_text(self, text, is_pop=True):
        if is_pop:
            self.in_changes += 1
        self.input_view.run_command("select_all")
        self.input_view.run_command("left_delete")
        self.input_view.run_command("insert", {"characters": text})

    def not_in_error(self):
        si = self.current
        #while si and not si.regions and si.search:
        while si and not si.selected and si.search:
            si = si.prev
        return si

    def finish(self, abort=False):
        if self.current and self.current.search:
            ISearchInfo.last_search = self.current.search
        self.jove.set_status("")

        point_set = False
        if not abort:
            selection = self.view.sel()
            selection.clear()
            current = self.not_in_error()
            if current and current.selected:
                selection.add_all(current.selected)
                point_set = True

        if not point_set:
            # back whence we started
            self.jove.set_selection(self.point)
            self.jove.ensure_visible(self.point)
        else:
            self.jove.set_mark(self.point, and_selection=False)

        # erase our regions
        self.view.erase_regions("find")
        self.view.erase_regions("selected")

    def update(self):
        si = self.not_in_error()
        if si is None:
            return

        self.view.add_regions("find", si.regions, "text", "", sublime.DRAW_NO_FILL)
        selected = si.selected or []
        self.view.add_regions("selected", selected, "string", "", 0)
        if selected:
            self.jove.ensure_visible(selected[-1])

        status = ""
        if si != self.current:
            status += "Failing "
        if self.current.wrapped:
            status += "Wrapped "
        status += "I-Search " + ("Forward" if self.current.forward else "Reverse")
        if si != self.current:
            if len(self.current.regions) > 0:
                status += " %s matches %s" % (len(self.current.regions), ("above" if self.forward else "below"))
        else:
            status += " %d matches, %d cursors" % (len(si.regions), len(si.selected))

        self.jove.set_status(status)

    #
    # Try to make progress with the current search string. Even if we're currently failing (in our
    # current direction) it doesn't mean there aren't matches for what we've typed so far.
    #
    def next(self, keep, forward=None):
        if self.current.prev is None:
            # do something special if we invoke "i-search" twice at the beginning
            if ISearchInfo.last_search:
                # insert the last search string
                self.set_text(ISearchInfo.last_search, is_pop=False)
        else:
            if forward is None:
                forward = self.current.forward
            new = self.current.step(forward=forward, keep=keep)
            if new:
                self.push(new)
                self.update()

    def keep_all(self):
        while self.current.regions and self.current.current_index < len(self.current.regions):
            new = self.current.step(forward=self.current.forward, keep=True)
            if new:
                self.push(new)
            else:
                break
        self.update()

    def append_from_cursor(self):
        # Figure out the contents to the right of the last region in the current selected state, and
        # append characters from there.
        si = self.current
        if len(si.search) > 0 and not si.selected:
            # search is failing - no point in adding from current cursor!
            return

        view = self.view
        limit = view.size()
        if si.selected:
            # grab end of most recent item
            point = si.selected[-1].end()
        else:
            point = self.point
        if point >= limit:
            return

        # now push new states for each character we append to the search string
        helper = self.jove
        search = si.search
        separators = view.settings().get("jove_word_separators", default_jove_word_separators)
        case_sensitive = re.search(r'[A-Z]', search) is not None

        def append_one(ch):
            if not case_sensitive:
                ch = ch.lower()
            if self.regex and ch in "{}()[].*+":
                return "\\" + ch
            return ch

        if point < limit:
            # append at least one character, word character or not
            search += append_one(view.substr(point))
            point += 1
            self.on_change(search)

            # now insert word characters
            while point < limit and helper.is_word_char(point, True, separators):
                ch = view.substr(point)
                search += append_one(ch)
                self.on_change(search)
                point += 1
        self.set_text(self.current.search)

    def cancel(self):
        self.view.window().run_command("hide_panel")
        self.finish(abort=True)

    def quit(self):
        close = False

        if self.current.regions:
            # if we have some matched regions, we're in "successful" state and close down the whole
            # thing
            close = True
        else:
            # here the search is currently failing, so we back up until the last non-failing state
            while self.current.prev and not self.current.prev.regions:
                self.current = self.current.prev
            if self.current.prev is None:
                close = True
        if close:
            self.cancel()
        else:
            self.pop()


    def find_closest(self, regions, pos, forward):
        #
        # The regions are sorted so clearly this would benefit from a simple binary search ...
        #
        if len(regions) == 0:
            return -1
        # find the first region after the specified pos
        found = False
        if forward:
            for index,r in enumerate(regions):
                if r.end() >= pos:
                    return index
            return -1
        else:
            for index,r in enumerate(regions):
                if r.begin() > pos:
                    return index - 1
            return len(regions) - 1

class JoveIncSearchCommand(JoveTextCommand):
    def run_cmd(self, jove, cmd=None, **kwargs):
        info = ViewState.isearch_info
        if info is None:
            regex = kwargs.get('regex', False)
            if jove.state.argument_supplied:
                regex = not regex
            info = ViewState.isearch_info = ISearchInfo(self.view, kwargs['forward'], regex)
            info.open()

        else:
            if cmd == "next":
                info.next(**kwargs)
            elif cmd == "pop":
                info.pop()
            elif cmd == "append_from_cursor":
                info.append_from_cursor()
            elif cmd == "keep_all":
                info.keep_all()
            else:
                print("Not handling cmd", cmd, kwargs)

    def is_visible(self, **kwargs):
        # REMIND: is it not possible to invoke isearch from the menu for some reason. I think the
        # problem is that a focus thing is happening and we're dismissing ourselves as a result. So
        # for now we hide it.
        if ViewState.isearch_info:
            return False
        return False


class JoveIncSearchEscape(JoveTextCommand):
    unregistered = True
    def run_cmd(self, jove, next_cmd, next_args):
        info = ViewState.isearch_info
        info.done()
        info.view.run_command(next_cmd, next_args)

#
# Indent for tab command. If the cursor is not within the existing indent, just call reindent. If
# the cursor is within the indent, move to the start of the indent and call reindent. If the cursor
# was already at the indent didn't change after calling reindent, indent one more level.
#
class JoveIndentCommand(JoveTextCommand):
    def run_cmd(self, jove):
        point = jove.get_point()
        indent,cursor = jove.get_line_indent(point)
        jove.set_status("Indent at %s, within=%s, at=%s" % (indent, cursor <= indent, cursor == indent))
        if cursor > indent:
            jove.run_command("reindent", {})
        else:
            if cursor < indent:
                jove.run_command("move_to", {"to": "bol", "extend": False})
            jove.run_command("reindent", {})

            # now check to see if we moved, and if not, indent one more level
            if indent == cursor:
                new_indent,new_cursor = jove.get_line_indent(jove.get_point())
                if new_indent == indent:
                    # cursor was already at the indent
                    jove.run_command("indent", {})

class JoveQuitCommand(JoveTextCommand):
    def run_cmd(self, jove):
        window = self.view.window()

        if ViewState.isearch_info:
            ViewState.isearch_info.quit()
            return

        for cmd in ['clear_fields', 'hide_overlay', 'hide_auto_complete', 'hide_panel']:
            window.run_command(cmd)

        # If there is a selection, set point to the end of it that is visible.
        s = self.view.sel()
        s = s and s[0]
        if s:
            if jove.is_visible(s.b):
                pos = s.b
            elif jove.is_visible(s.a):
                pos = s.a
            else:
                # set point to the beginning of the line in the middle of the window
                visible = self.view.visible_region()
                top_line = self.view.rowcol(visible.begin())[0]
                bottom_line = self.view.rowcol(visible.end())[0]
                pos = self.view.text_point((top_line + bottom_line) / 2, 0)
            jove.set_selection(pos, pos)
        if jove.state.active_mark:
            jove.toggle_active_mark_mode()

class JoveConvertPlistToJsonCommand(JoveTextCommand):
    JSON_SYNTAX = "Packages/Javascript/JSON.tmLanguage"
    PLIST_SYNTAX = "Packages/XML/XML.tmLanguage"

    def run_cmd(self, jove):
        import json
        from plistlib import readPlistFromBytes, writePlistToBytes

        data = self.view.substr(sublime.Region(0, self.view.size())).encode("utf-8")
        self.view.replace(jove.edit, sublime.Region(0, self.view.size()),
                          json.dumps(readPlistFromBytes(data), indent=4, separators=(',', ': ')))
        self.view.set_syntax_file(JSON_SYNTAX)

class JoveConvertJsonToPlistCommand(JoveTextCommand):
    JSON_SYNTAX = "Packages/Javascript/JSON.tmLanguage"
    PLIST_SYNTAX = "Packages/XML/XML.tmLanguage"

    def run_cmd(self, jove):
        import json
        from plistlib import readPlistFromBytes, writePlistToBytes

        data = json.loads(self.view.substr(sublime.Region(0, self.view.size())))
        self.view.replace(jove.edit, sublime.Region(0, self.view.size()), writePlistToBytes(data).decode("utf-8"))
        self.view.set_syntax_file(PLIST_SYNTAX)

#
# Function to dedup views in all the groups of the specified window. This does not close views that
# have changes because that causes a warning to popup. So we have a monitor which dedups views
# whenever a file is saved in order to dedup them then when it's safe.
#
def dedup_views(window):
    group = window.active_group()
    for g in range(window.num_groups()):
        found = dict()
        views = window.views_in_group(g)
        active = window.active_view_in_group(g)
        for v in views:
            if v.is_dirty():
                # we cannot nuke a dirty buffer or we'll get an annoying popup
                continue
            id = v.buffer_id()
            if id in found:
                if v == active:
                    # oops - nuke the one that's already been seen and put this one in instead
                    before = found[id]
                    found[id] = v
                    v = before
                window.focus_view(v)
                window.run_command('close')
            else:
                 found[id] = v
        window.focus_view(active)
    window.focus_group(group)

def InitModule(module_name):
    def get_cmd_name(cls):
        name = cls.__name__
        name = re.sub('(?!^)([A-Z]+)', r'_\1', name).lower()
        # strip "_command"
        return name[0:len(name) - 8]

    module = sys.modules[module_name]
    for name in dir(module):
        if name.startswith("Jove"):
            cls = getattr(module, name)
            try:
                if not issubclass(cls, sublime_plugin.TextCommand):
                    continue
            except:
                continue
            # see what the deal is
            name = get_cmd_name(cls)
            cls.jove_cmd_name = name
            if cls.is_kill_cmd:
                kill_cmds.add(name)
            if cls.is_ensure_visible_cmd:
                ensure_visible_cmds.add(name)

InitModule(__name__)
