#!/usr/bin/python3

from telegram.ext import Updater, CommandHandler
import mpd
import random
import os
import sys
import select
from gi.repository import GObject as gobject
import time
import gettext
import config

from signal import signal, SIGINT, SIGTERM, SIGABRT

import logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

_ = gettext.gettext

def basename_noext(name):
    return os.path.splitext(os.path.basename(name))[0]

def is_vocal(song):
    lower = song.lower()
    return 'vocal' in lower or 'inst' in lower or 'tv size' in lower

def format_path(path, *path2):
    return os.path.normpath(os.path.join(path.strip('/'), *path2))

def format_song_info(song_info):
    first = True
    result = ''
    for field in ['title', 'album', 'artist']:
        if field in song_info:
            if not first:
                result += " - ";
            else:
                first = False
            result += song_info[field]
    return result

def access_mpd(retry=False):
    def access_mpd_decorator(func):
        def wrap(self, *vargs, **kwargs):
            try:
                return func(self, *vargs, **kwargs)
            except (mpd.ConnectionError, IOError, BrokenPipeError):
                pass

            try:
                self.disconnect()
                self.connect()
                if retry:
                    return func(self, *vargs, **kwargs)
            except (mpd.ConnectionError, IOError, BrokenPipeError):
                pass
            
        return wrap
    return access_mpd_decorator

def string_arg_checker(default=None):
    def string_arg_checker_decorator(func):
        def wrap(self, args):
            if args or default is not None:
                if args:
                    command = self.update.message.text.split(' ')[0]
                    string_arg = self.update.message.text[len(command) + 1:]
                else:
                    string_arg = str(default)
                return func(self, string_arg)
            else:
                return self.send_text(_("Missing argument."))
        return wrap
    return string_arg_checker_decorator

def args_checker(num, defaults=None):
    def args_checker_decorator(func):
        def wrap(self, args):
            if len(args) >= num:
                # make a copy
                args = list(args)
                if defaults is not None:
                    for idx, default in enumerate(defaults):
                        if num + idx >= len(args):
                            args.append(default)
                return func(self, args)
            else:
                return self.send_text(_("Missing argument."))
        return wrap

    return args_checker_decorator

def command_handler(check_super_user = False):
    def command_handler_decorator(func):
        def wrap(self, bot, update, *vargs, **kwargs):
            self.bot = bot
            self.update = update
            command = update.message.text[1:].split(' ')[0].split('@')
            if len(command) > 1 and command[1] != bot.username:
                return
            if not check_super_user or self.config['SUPER_USER'] == self.update.message.from_user.username:
                return func(self, *vargs, **kwargs)
            else:
                return self.send_text(_("You're not super user."))
        return wrap
    return command_handler_decorator


class Quota(object):
    limit = 5

    def __init__(self, username):
        self.history = []
        self.username = username

    def order(self, song):
        now = time.time()
        self.history.append((song, now))
        if len(self.history) > self.limit:
            self.history.pop(0)

    def refresh(self):
        now = time.time()
        self.history = [hist for hist in self.history if hist[1] + 3600 < now]

    def can_order(self):
        return len(self.history) < self.limit

class MPDDJ(object):
    def __init__(self, config):
        self.config = config
        self.cached_songs = []
        self.client = mpd.MPDClient()
        self.client.timeout = 5
        self.idle_client = mpd.MPDClient()
        self.idle_client.timeout = 5
        self.connected = False
        self.io_source = None
        self.timeout_source = None

        self.quota = dict()

        self.updater = Updater(self.config['TOKEN'])

        self.updater.dispatcher.add_handler(CommandHandler('start', self.start))
        self.updater.dispatcher.add_handler(CommandHandler('add', self.add, pass_args=True))
        self.updater.dispatcher.add_handler(CommandHandler('sample', self.sample))
        self.updater.dispatcher.add_handler(CommandHandler('status', self.status))
        self.updater.dispatcher.add_handler(CommandHandler('stats', self.stats))
        self.updater.dispatcher.add_handler(CommandHandler('order', self.order, pass_args=True))
        self.updater.dispatcher.add_handler(CommandHandler('searchorder', self.searchorder, pass_args=True))
        self.updater.dispatcher.add_handler(CommandHandler('history', self.history))
        self.updater.dispatcher.add_handler(CommandHandler('search', self.search, pass_args=True))
        self.updater.dispatcher.add_handler(CommandHandler('searchadd', self.searchadd, pass_args=True))
        self.updater.dispatcher.add_handler(CommandHandler('playlist', self.playlist))
        self.updater.dispatcher.add_handler(CommandHandler('list', self.list_files, pass_args=True))
        self.updater.dispatcher.add_handler(CommandHandler('stream', self.stream))
        self.updater.dispatcher.add_handler(CommandHandler('help', self.help))

    def send_text(self, text):
        self.bot.sendMessage(self.update.message.chat_id, text=text, reply_to_message_id=self.update.message.message_id)

    def reconnect(self):
        self.connect()
        return False

    def connect(self):
        try:
            self.client.connect(host=self.config['HOST'], port=self.config['PORT'])
            self.idle_client.connect(host=self.config['HOST'], port=self.config['PORT'])
            if self.config['PASSWORD']:
                self.client.password(self.config['PASSWORD'])
                self.idle_client.password(self.config['PASSWORD'])
            self.client.consume(1)
            self.client.random(0)
            self.refresh_cache()
            self.fill_song()
            status = self.client.status()
            if status['state'] != 'play':
                self.client.play()
            self.io_source = gobject.io_add_watch(self.idle_client, gobject.IO_IN, self.idle_callback)
            self.idle_client.send_idle(*self.watch)
            self.timeout_source = gobject.timeout_add(60000, self.fill_song)
            self.connected = True
        except (IOError, mpd.CommandError):
            gobject.timeout_add(5000, self.reconnect)

    def disconnect(self):
        self.client.disconnect()
        self.idle_client.disconnect()
        if self.io_source:
            gobject.source_remove(self.io_source)
            self.io_source = None
        if self.timeout_source:
            gobject.source_remove(self.timeout_source)
            self.timeout_source = None
        self.connected = False

    def alone(self):
        playlist = self.client.playlistinfo()
        files = {song_info['file'] for song_info in playlist}
        for quota in self.quota.values():
            for (song, time) in quota.history:
                if song in files:
                    return False
        return True


    def refresh_cache(self):
        cached_songs = self.client.list('file')
        self.cached_songs = []
        for song in cached_songs:
            # skip off vocal and instrumental
            if is_vocal(song):
                continue
            self.cached_songs.append(song)

    @access_mpd(True)
    @command_handler()
    def status(self):
        song_info = self.client.currentsong()
        if "title" not in song_info:
            self.send_text(_("Nothing to play"))
        else:
            self.send_text(_("Now Playing:\n{0}").format(format_song_info(song_info)))

    @access_mpd(True)
    @command_handler()
    def stats(self):
        stats = self.client.stats()
        self.send_text(_("Number of Songs: {0}\nNumber of Albums: {1}").format(stats["songs"], stats["albums"]))

    @access_mpd(True)
    @command_handler()
    @args_checker(1, ("0",))
    def search(self, args):
        limit = 0
        try:
            limit = int(args[1])
        except:
            pass
        try:
            songs = self.client.search('any', args[0])[limit:limit+5]
            if songs:
                self.send_text("\n".join(song_info['file'] for song_info in songs))
            else:
                self.send_text(_("No matched song."))
        except mpd.CommandError as e:
            self.send_text(_("Search failed. Error: {0}").format(e))

    def add_song(self, song):
        try:
            self.client.add(song)
            self.send_text(_("Adding song {0}").format(song))
        except mpd.CommandError as e:
            self.send_text(_("Adding song {0} failed {1}").format(song, e))

    @access_mpd(True)
    @command_handler(check_super_user=True)
    @args_checker(1)
    def searchadd(self, args):
        songs = self.client.search('any', args[0])
        if songs:
            self.add_song(songs[0]['file'])
        else:
            self.send_text(_("No matched song."))

    @access_mpd(True)
    @command_handler()
    @args_checker(1)
    def searchorder(self, args):
        songs = self.client.search('any', args[0])
        if songs:
            self.order_song([song_info['file'] for song_info in songs if 'file' in song_info])
        else:
            self.send_text(_("No matched song."))

    @command_handler()
    def sample(self):
        if self.cached_songs:
            self.send_text('\n'.join(random.sample(self.cached_songs, min(len(self.cached_songs), 5))))
        else:
            self.send_text(_("No matched song."))

    @access_mpd(True)
    @command_handler()
    @string_arg_checker("/")
    def list_files(self, path):
        files = self.client.listfiles(path)
        if files:
            result = "\n".join([format_path(path, item["directory"]) + '/' for item in files if 'directory' in item and not item['directory'].startswith('.')] + [format_path(path, item["file"]) for item in files if 'file' in item and not item['file'].startswith('.') and item['file'].endswith('.mp3')])
            self.send_text(result)
        else:
            self.send_text(_("No matched path."))

    @command_handler()
    def start(self):
        self.send_text(_('Welcome to MPD DJ!'))

    @access_mpd(True)
    @command_handler(check_super_user=True)
    @string_arg_checker()
    def add(self, path):
        self.add_song(path)

    @command_handler()
    def stream(self):
        self.send_text(_("Play stream via: {0}").format(self.config['STREAM_URL']))

    @access_mpd(True)
    @command_handler()
    def playlist(self):
        playlist = self.client.playlistinfo()
        if playlist:
            self.send_text("\n".join(format_song_info(song_info) for song_info in playlist))
        else:
            self.send_text(_('Nothing :('))

    def get_quota(self, user):
        if user not in self.quota:
            self.quota[user] = Quota(user)
        return self.quota[user]

    def order_song(self, song):
        quota = self.get_quota(self.update.message.from_user.username)
        if quota.can_order() or quota.username == self.config['SUPER_USER'] or self.alone():
            try:
                playlist = self.client.playlistinfo()
                if len(playlist) > 15:
                    self.send_text(_('Too many songs already, please wait.'))
                    return
                if isinstance(song, list):
                    files = {song_info['file'] for song_info in playlist}
                    match_song = None
                    for cand_song in song:
                        if cand_song not in files and not is_vocal(cand_song):
                            match_song = cand_song
                    if not match_song:
                        self.send_text(_('This song is already ordered.'))
                        return
                    song = match_song
                else:
                    if any(song_info['file'] == song for song_info in playlist):
                        self.send_text(_('This song is already ordered.'))
                        return
                self.client.add(song)
                quota.order(song)
                self.send_text(_('Ordering {0}').format(song))
            except mpd.CommandError as e:
                self.send_text(_('Failed to order {0}').format(e))
        else:
            self.send_text(_("Quota used up."))


    @command_handler()
    @string_arg_checker('')
    def order(self, path):
        if path == '' and self.cached_songs:
            path = random.choice(self.cached_songs)
        self.order_song(path)

    @command_handler()
    def history(self):
        history = []
        for quota in self.quota.values():
            for hist in quota.history:
                history.append((quota.username, hist))

        # sort by time, top 10
        recent = sorted(history, key=lambda hist: -hist[1][1])[0:10]
        if recent:
            self.send_text('\n'.join('@{0}: {1}'.format(username, basename_noext(song)) for username, (song, time) in reversed(recent)))
        else:
            self.send_text(_('No history yet.'))

    @command_handler()
    def help(self):
        help_text = [
            _("start - Description"),
            _("add - Add song"),
            _("sample - Randomly list some songs"),
            _("status - DJ Status"),
            _("stats - DJ stats"),
            _("order - Order a song"),
            _("searchorder - Search and order first match song"),
            _("history - Order history"),
            _("search - Search song"),
            _("searchadd - Search and add first match song"),
            _("playlist - Show current playlist"),
            _("list - List files"),
            _("stream - Get stream address"),
            _("help - Show this help"),
        ]
        self.send_text("\n".join(help_text))

    def rebuild_state(self):
        pass

    watch = ('database', 'playlist')

    def fill_song(self):
        playlist = self.client.playlistinfo()
        if len(playlist) <= 1 and self.cached_songs:
            song = random.choice(self.cached_songs)
            self.client.add(song)
            self.client.play()

    @access_mpd()
    def idle_callback(self, source, condition):
        changes = self.idle_client.fetch_idle()
        print(changes)
        for c in changes:
            if c == 'database':
                self.refresh_cache()
            elif c == 'playlist':
                self.fill_song()
        self.idle_client.send_idle(*self.watch)
        return True

    def refresh_quota(self):
        for quota in self.quota.values():
            quota.refresh()

    def run(self):
        for sig in [SIGINT, SIGTERM, SIGABRT]:
            signal(sig, self.signal_handler)

        self.connect()
        self.updater.start_polling()

        gobject.timeout_add(60000, self.refresh_quota)

        try:
            gobject.MainLoop().run()
        except (KeyboardInterrupt, SystemExit):
            gobject.MainLoop().quit()
            self.updater.stop()

    def signal_handler(self, signum, frame):
        gobject.MainLoop().quit()
        self.updater.signal_handler(signum, frame)

gettext.bindtextdomain('mpddj', localedir=os.path.join(os.path.dirname(os.path.abspath(os.path.realpath(sys.argv[0] or 'locale'))), 'locale'))
gettext.textdomain('mpddj')

MPDDJ(config.config).run()
