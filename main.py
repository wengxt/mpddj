#!/usr/bin/python3

from telegram.ext import Updater, CommandHandler
import pickle
import musicpd
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
            except (musicpd.ConnectionError, IOError, BrokenPipeError):
                pass
            except musicpd.MPDError:
                return

            try:
                logging.log(logging.DEBUG, "Try Reconnect")
                self.disconnect()
                self.connect()
                if retry:
                    logging.log(logging.DEBUG, "Retry command")
                    return func(self, *vargs, **kwargs)
            except (musicpd.ConnectionError, IOError, BrokenPipeError) as e:
                logging.log(logging.DEBUG, "{0}".format(e))
            
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

    def __repr__(self):
        return "Quota({0}, {1}".format(self.username, self.history)

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
        self.history = [hist for hist in self.history if hist[1] + 3600 > now]

    def can_order(self):
        return len(self.history) < self.limit

class MPDDJ(object):
    def __init__(self, config):
        self.config = config
        self.cached_songs = []
        self.client = musicpd.MPDClient()
        self.client.timeout = 5
        self.idle_client = musicpd.MPDClient()
        self.idle_client.timeout = 5
        self.connected = False
        self.io_source = None
        self.timeout_source = None

        self.quota = None
        try:
            with open('history.pickle', 'rb') as handle:
                self.quota = pickle.load(handle)
                print(self.quota)
        except Exception as e:
            logging.log(logging.DEBUG, "LOAD Hist : {0}".format(e))
        print(self.quota)
        if not isinstance(self.quota, dict):
            self.quota = dict()
        print(self.quota)

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
        logging.log(logging.DEBUG, "reconnect")
        self.disconnect()
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
        except (IOError, musicpd.ConnectionError, musicpd.CommandError):
            logging.log(logging.DEBUG, "Place reconnect")
            gobject.timeout_add(5000, self.reconnect)
        except Exception as e:
            logging.log(logging.DEBUG, "CONECT ERROR: {0}".format(e))

    def disconnect(self):
        try:
            self.client.disconnect()
        except:
            pass
        try:
            self.idle_client.disconnect()
        except:
            pass
        if self.io_source is not None:
            gobject.source_remove(self.io_source)
            self.io_source = None
        if self.timeout_source is not None:
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

    def search_helper(self, args):
        search_args = []
        for arg in args:
            search_args.append('any')
            search_args.append(arg)
        return self.client.search(*search_args)

    @access_mpd(True)
    @command_handler()
    @args_checker(1)
    def search(self, args):
        limit = 0
        if len(args) > 1:
            try:
                limit = int(args[-1])
                args.pop()
            except:
                pass
        try:
            songs = self.search_helper(args)[limit:limit+5]
            if songs:
                self.send_text("\n".join(song_info['file'] for song_info in songs))
            else:
                self.send_text(_("No matched song."))
        except musicpd.CommandError as e:
            self.send_text(_("Search failed. Error: {0}").format(e))

    def add_song(self, song):
        try:
            self.client.add(song)
            self.send_text(_("Adding song {0}").format(song))
        except musicpd.CommandError as e:
            self.send_text(_("Adding song {0} failed {1}").format(song, e))

    @access_mpd(True)
    @command_handler(check_super_user=True)
    @args_checker(1)
    def searchadd(self, args):
        songs = self.search_helper(args)
        if songs:
            self.add_song(songs[0]['file'])
        else:
            self.send_text(_("No matched song."))

    @access_mpd(True)
    @command_handler()
    @args_checker(1)
    def searchorder(self, args):
        songs = self.search_helper(args)
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
        files = self.client.lsinfo(path.strip('/'))
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

    def is_ordered(self, song):
        for quota in self.quota.values():
            for (_song, time) in quota.history:
                if song == _song:
                    return True

        return False

    def order_song(self, song):
        quota = self.get_quota(self.update.message.from_user.username)
        alone = self.alone()
        if quota.can_order() or quota.username == self.config['SUPER_USER'] or alone:
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
                quota.order(song)
                self.client.command_list_ok_begin()
                prefix = ''
                if alone:
                    prefix = _('So lonely.. let me play for you immediately.') + '\n'
                    self.client.addid(song, 0)
                    self.client.play(0)
                    self.client.delete((1,))
                else:
                    if len(playlist) == 2 and not self.is_ordered(playlist[1]['file']):
                        self.client.addid(song, 1)
                        self.client.delete((2,))
                    else:
                        self.client.add(song)
                self.client.command_list_end()
                self.send_text(prefix + _('Ordering {0}').format(song))
            except musicpd.CommandError as e:
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
        print(self.quota)
        for quota in self.quota.values():
            for hist in quota.history:
                history.append((quota.username, hist))

        print(history)
        # sort by time, top 10
        recent = sorted(history, key=lambda hist: -hist[1][1])[0:10]
        print(recent)
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
        logging.log(logging.DEBUG, 'refresh_Quota')
        for quota in self.quota.values():
            quota.refresh()

    def run(self):
        for sig in [SIGINT, SIGTERM, SIGABRT]:
            signal(sig, self.signal_handler)

        self.connect()
        self.updater.start_polling()

        gobject.timeout_add(6000, self.refresh_quota)

        try:
            gobject.MainLoop().run()
        except (KeyboardInterrupt, SystemExit):
            logging.log(logging.DEBUG, "Exit")
            gobject.MainLoop().quit()
            self.updater.stop()
            with open('history.pickle', 'wb') as handle:
                pickle.dump(self.quota, handle)

    def signal_handler(self, signum, frame):
        gobject.MainLoop().quit()
        self.updater.signal_handler(signum, frame)

gettext.bindtextdomain('mpddj', localedir=os.path.join(os.path.dirname(os.path.abspath(os.path.realpath(sys.argv[0] or 'locale'))), 'locale'))
gettext.textdomain('mpddj')

MPDDJ(config.config).run()
