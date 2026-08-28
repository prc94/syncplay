import ast
import collections
import hashlib
import os
import os.path
import random
import re
import sys
import threading
import time
from copy import deepcopy
from functools import wraps
from urllib.parse import urlparse

from twisted.application.internet import ClientService
from twisted.internet.endpoints import HostnameEndpoint
from twisted.internet.protocol import ClientFactory
from twisted.internet import reactor, task, defer, threads

try:
    SSL_CERT_FILE = None
    import certifi
    import pem
    from twisted.internet.ssl import Certificate, optionsForClientTLS, trustRootFromCertificates
    certPath = certifi.where()
    if os.path.exists(certPath):
        SSL_CERT_FILE = certPath
    elif 'zip' in certPath:
        import tempfile
        import zipfile
        zipPath, memberPath = certPath.split('.zip/')
        zipPath += '.zip'
        archive = zipfile.ZipFile(zipPath, 'r')
        tmpDir = tempfile.gettempdir()
        extractedPath = archive.extract(memberPath, tmpDir)
        SSL_CERT_FILE = extractedPath
except:
    pass

from syncplay import utils, constants, version
from syncplay.constants import PRIVACY_SENDHASHED_MODE, PRIVACY_DONTSEND_MODE, \
    PRIVACY_HIDDENFILENAME
from syncplay.messages import getMissingStrings, getMessage, isNoOSDMessage
from syncplay.protocols import SyncClientProtocol
from syncplay.watched import WatchedManager
from syncplay.utils import isMacOS
class SyncClientFactory(ClientFactory):
    def __init__(self, client, retry=constants.RECONNECT_RETRIES):
        self._client = client
        self.retry = retry
        self._timesTried = 0

    def buildProtocol(self, addr):
        self._timesTried = 0
        return SyncClientProtocol(self._client)

    def stopRetrying(self):
        self._client._reconnectingService.stopService()
        self._client.ui.showErrorMessage(getMessage("disconnection-notification"))


class SyncplayClient(object):
    def __init__(self, playerClass, ui, config):
        self.delayedLoadPath = None
        constants.SHOW_OSD = config['showOSD']
        constants.SHOW_OSD_WARNINGS = config['showOSDWarnings']
        constants.SHOW_SLOWDOWN_OSD = config['showSlowdownOSD']
        constants.SHOW_DIFFERENT_ROOM_OSD = config['showDifferentRoomOSD']
        constants.SHOW_SAME_ROOM_OSD = config['showSameRoomOSD']
        constants.SHOW_DURATION_NOTIFICATION = config['showDurationNotification']
        constants.SHOW_PLAYLIST_SKIP_WARNINGS = config['showPlaylistSkipWarnings']
        constants.SHOW_PLAYLIST_ORDER_WARNINGS = config['showPlaylistOrderWarnings']
        constants.DEBUG_MODE = config['debug']
        constants.FOLDER_SEARCH_FIRST_FILE_TIMEOUT = config['folderSearchFirstFileTimeout']
        constants.FOLDER_SEARCH_TIMEOUT = config['folderSearchTimeout']
        constants.FOLDER_SEARCH_DOUBLE_CHECK_INTERVAL = config['folderSearchDoubleCheckInterval']
        constants.FOLDER_SEARCH_WARNING_THRESHOLD = config['folderSearchWarningThreshold']

        watchedName = config['watchedSubfolder'] or ""
        watchedName = watchedName.strip()
        if watchedName in (".", ".."):
            watchedName = ""
        elif watchedName:
            if os.path.sep in watchedName or (os.path.altsep and os.path.altsep in watchedName):
                watchedName = os.path.basename(watchedName)

        constants.WATCHED_SUBFOLDER = watchedName
        constants.WATCHED_AUTOMOVE = config['watchedAutoMove'] if len(constants.WATCHED_SUBFOLDER) > 0 else False
        constants.WATCHED_AUTOCREATESUBFOLDERS = config['watchedSubfolderAutocreate'] if len(constants.WATCHED_SUBFOLDER) > 0 else False

        constants.WATCHED_HISTORY_ENABLED = config['watchedHistoryEnabled']
        constants.AUTO_REMOVE_WATCHED_FROM_PLAYLIST = config['autoRemoveWatchedFromPlaylist']
        constants.WATCHED_HISTORY_FILENAME = constants.WATCHED_HISTORY_FILENAME_DEFAULT

        self.controlpasswords = {}
        self.lastControlPasswordAttempt = None
        self.serverVersion = "0.0.0"

        self.serverFeatures = {}

        self.lastRewindTime = None
        self.lastUpdatedFileTime = None
        self.lastAdvanceTime = None
        self.fileOpenBeforeChangingPlaylistIndex = None
        self.waitingToLoadNewfile = False
        self.waitingToLoadNewfileSince = None
        self.lastConnectTime = None
        self.lastSetRoomTime = None
        self.hadFirstPlaylistIndex = False
        self.hadFirstStateUpdate = False
        self.lastLeftTime = 0
        self.lastPausedOnLeaveTime = None
        # One-shot: set when the AFK keybind issues its "step away" pause, consumed when that
        # pause is observed so it bypasses the readiness-toggle-on-pause machinery (going AFK is
        # already not-ready; letting _toggleReady run would race with the Set:afk echo and could
        # clear the AFK we just set or flip readiness).
        self._afkKeybindPausePending = False
        self.lastLeftUser = ""
        self.protocolFactory = SyncClientFactory(self)
        self.ui = UiManager(self, ui)
        self.userlist = SyncplayUserlist(self.ui, self)
        self._protocol = None
        """:type : SyncClientProtocol|None"""
        self._player = None
        if config['room'] is None or config['room'] == '':
            config['room'] = config['name']  # ticket #58
        self.defaultRoom = config['room']
        self.playerPositionBeforeLastSeek = 0.0
        self.setUsername(config['name'])
        self.setRoom(config['room'])
        if config['password']:
            config['password'] = hashlib.md5(config['password'].encode('utf-8')).hexdigest()
        self._serverPassword = config['password']
        self._host = "{}:{}".format(config['host'], config['port'])
        self._publicServers = config["publicServers"]
        if not config['file']:
            self.__getUserlistOnLogon = True
        else:
            self.__getUserlistOnLogon = False
        self._playerClass = playerClass
        # Whether this client's player can render the live yap-timer overlay. Derived from the player
        # class (known now, before the player process starts) so it can be advertised in the Hello.
        self._yapTimerOSDSupported = getattr(playerClass, "yapTimerOSDSupported", False)
        self._pauseWarningOSDSupported = getattr(playerClass, "pauseWarningOSDSupported", False)
        self._genericOSDSupported = getattr(playerClass, "genericOSDSupported", False)
        self._trackProposalsSupported = getattr(playerClass, "trackProposalsSupported", False)
        self._serverTrustedDomains = []  # Session-only overlay of admin-published trusted domains
        self._shareTrustedDomainsOnUpdate = False  # Session-only: auto-publish own list when it changes
        self._config = config

        self._running = False
        self._askPlayerTimer = None

        self._lastPlayerUpdate = None
        self._playerPosition = 0.0
        self._playerPaused = True

        self._lastGlobalUpdate = None
        self._globalPosition = 0.0
        self._globalPaused = 0.0
        self._syncedWithRoomSinceConnect = False  # Cleared on (re)connect; set once our player has actually reached the room position
        self._suppressSyncOnNextFileLoad = False  # A file deliberately opened at the start (playlist switch) must not be seeked to the room
        self._userOffset = 0.0
        self._speedChanged = False
        self.behindFirstDetected = None
        self._desyncSince = {}  # Desync condition -> when it was first continuously observed; see _desyncSustainedFor
        # Buffer hold (see docs/buffer-pause.md): our own player stalling on a cache, and the room
        # being held for somebody's stall. Both suppress the desync reactions above.
        self._buffering = False
        self._bufferingSince = None
        self._bufferCachePercent = None
        self._stallReference = None  # (time, position) baseline the stall heuristic measures progress against
        self._bufferHoldActive = False  # A server-driven hold is in force for this room
        self._bufferFallbackPaused = False  # We paused the room ourselves because the server has no buffer hold
        self._lastBufferChatTime = None  # Rate limit on our own "I am buffering" chat lines
        self.autoPlay = False
        self.autoPlayThreshold = None

        self.autoplayTimer = task.LoopingCall(self.autoplayCountdown)
        self.autoplayTimeLeft = constants.AUTOPLAY_DELAY

        self.__playerReady = defer.Deferred()

        self._warnings = self._WarningManager(self._player, self.userlist, self.ui, self)
        self.fileSwitch = FileSwitchManager(self)
        self.playlist = SyncplayPlaylist(self)
        self.watched = WatchedManager(self)
        self.playlistMayNeedRestoring = False

        self._serverSupportsTLS = True

        if constants.LIST_RELATIVE_CONFIGS and 'loadedRelativePaths' in self._config and self._config['loadedRelativePaths']:
            paths = "; ".join(self._config['loadedRelativePaths'])
            self.ui.showMessage(getMessage("relative-config-notification").format(paths), noPlayer=True, noTimestamp=True)

        if constants.DEBUG_MODE and constants.WARN_ABOUT_MISSING_STRINGS:
            missingStrings = getMissingStrings()
            if missingStrings is not None and missingStrings != "":
                self.ui.showDebugMessage("MISSING/UNUSED STRINGS DETECTED:\n{}".format(missingStrings))

    def initProtocol(self, protocol):
        self._protocol = protocol
        # Drop stale domains here, at connectionMade - i.e. before any server message for the new
        # connection can be processed. Doing it from checkForFeatureSupport (on Hello) instead used
        # to wipe the domains the server pushes during the join, which it sends before its Hello.
        self._serverTrustedDomains = []
        self._lastGlobalUpdate = time.time()

    def destroyProtocol(self):
        if self._protocol:
            self._protocol.drop()
        self._protocol = None
        self._serverTrustedDomains = []  # session-only: never carries across a disconnect

    def initPlayer(self, player):
        self._player = player
        if not self._player.alertOSDSupported:
            constants.OSD_WARNING_MESSAGE_DURATION = constants.NO_ALERT_OSD_WARNING_DURATION
        self.scheduleAskPlayer()
        self.__playerReady.callback(player)

    def addPlayerReadyCallback(self, lambdaToCall):
        self.__playerReady.addCallback(lambdaToCall)

    def playerIsNotReady(self):
        return self._player is None

    def scheduleAskPlayer(self, when=constants.PLAYER_ASK_DELAY):
        self._askPlayerTimer = task.LoopingCall(self.askPlayer)
        self._askPlayerTimer.start(when)

    def askPlayer(self):
        if not self._running:
            return
        if self._player:
            self._player.askForStatus()
        self.checkIfConnected()

    def checkIfConnected(self):
        if self._lastGlobalUpdate and self._protocol and time.time() - self._lastGlobalUpdate > constants.PROTOCOL_TIMEOUT:
            protocol = self._protocol
            self._lastGlobalUpdate = None
            self.ui.showErrorMessage(getMessage("server-timeout-error"))
            protocol.abort()
            return False
        return True

    def _determinePlayerStateChange(self, paused, position):
        pauseChange = self.getPlayerPaused() != paused and self.getGlobalPaused() != paused
        _playerDiff = abs(self.getPlayerPosition() - position)
        _globalDiff = abs(self.getGlobalPosition() - position)
        seeked = _playerDiff > constants.SEEK_THRESHOLD and _globalDiff > constants.SEEK_THRESHOLD
        return pauseChange, seeked

    def rewindFile(self):
        self.setPosition(0)
        self.establishRewindDoubleCheck()

    def establishRewindDoubleCheck(self):
        if constants.DOUBLE_CHECK_REWIND:
            reactor.callLater(0.5, self.doubleCheckRewindFile,)
            reactor.callLater(1, self.doubleCheckRewindFile,)
            reactor.callLater(1.5, self.doubleCheckRewindFile,)
        return

    def doubleCheckRewindFile(self):
        if self.getStoredPlayerPosition() > 5:
            self.setPosition(0)
            self.ui.showDebugMessage("Rewinded after double-check")

    def isPlayingMusic(self):
        if self.userlist.currentUser.file:
            for musicFormat in constants.MUSIC_FORMATS:
                if self.userlist.currentUser.file['name'].lower().endswith(musicFormat):
                    return True

    def seamlessMusicOveride(self):
        return self.isPlayingMusic() and self._recentlyAdvanced()

    def updatePlayerStatus(self, paused, position):
        position -= self.getUserOffset()
        pauseChange, seeked = self._determinePlayerStateChange(paused, position)
        positionBeforeSeek = self._playerPosition
        self._playerPosition = position
        self._playerPaused = paused
        self._updateBufferingState(paused, position)
        if self._lastGlobalUpdate and self.userlist.currentUser.file \
                and abs(position - self.getGlobalPosition()) <= constants.CLIENT_SYNC_ON_FILE_LOAD_THRESHOLD:
            self._syncedWithRoomSinceConnect = True  # closes the join window: see _syncNewlyLoadedFileToRoom
        currentLength = self.userlist.currentUser.file["duration"] if self.userlist.currentUser.file else 0
        if pauseChange and paused and self._afkKeybindPausePending:
            # This pause is the AFK keybind stepping away, not a readiness action (nor a reason to
            # autoplay the next file): let it propagate as a normal state so the room pauses, but
            # skip the readiness-toggle-on-pause machinery, which would otherwise race with the
            # Set:afk echo and could clear the AFK we just set or flip readiness.
            self._afkKeybindPausePending = False
        elif (
            pauseChange and paused and currentLength > constants.PLAYLIST_LOAD_NEXT_FILE_MINIMUM_LENGTH
            and abs(position - currentLength) < constants.PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD
        ):
            self.playlist.advancePlaylistCheck()
        elif pauseChange and "readiness" in self.serverFeatures and self.serverFeatures["readiness"]:
            if (
                currentLength == 0 or currentLength == -1 or
                not (
                    not self.playlist.notJustChangedPlaylist() and
                    abs(position - currentLength) < constants.PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD
                )
            ):
                pauseChange = self._toggleReady(pauseChange, paused)

        self.watched.processQueue()
        self.currentlyPlayingFilename = self.userlist.currentUser.file["name"] if self.userlist.currentUser.file else None
        playingCurrentIndex = not self.playlist._notPlayingCurrentIndex()
        if playingCurrentIndex:
            self.playlist.recordPlayedNearEOF(paused, position)
        if self._lastGlobalUpdate:
            self._lastPlayerUpdate = time.time()
            if (pauseChange or seeked) and self._protocol:
                if self.recentlyRewound() or self._recentlyAdvanced():
                    self._protocol.sendState(self._globalPosition, self.getPlayerPaused(), False, None, True)
                    return
                if seeked:
                    self.playerPositionBeforeLastSeek = self.getGlobalPosition()
                self._protocol.sendState(self.getPlayerPosition(), self.getPlayerPaused(), seeked, None, True)

    def prepareToChangeToNewPlaylistItemAndRewind(self):
        self.ui.showDebugMessage("Preparing to change to new playlist index and rewind...")
        self.fileOpenBeforeChangingPlaylistIndex = self.userlist.currentUser.file["path"] if self.userlist.currentUser.file else None
        self.waitingToLoadNewfile = True
        self.waitingToLoadNewfileSince = time.time()
        position = self.getStoredPlayerPosition()
        currentLength = self.userlist.currentUser.file["duration"] if self.userlist.currentUser.file else 0
        if (
                position is not None and
                self.lastUpdatedFileTime is not None and
                time.time() - self.lastUpdatedFileTime >= constants.WATCHED_NEAR_EOF_MINIMUM_TIME and
                self.playlist.lastNearEOFPlayedTime >= constants.WATCHED_NEAR_EOF_MINIMUM_PLAYED_TIME and
                currentLength > constants.PLAYLIST_LOAD_NEXT_FILE_MINIMUM_LENGTH and
                abs(position - currentLength) < constants.PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD
        ):
            self.playlist.clearNearEOFMarker()
            self.watched.markCurrentFileWatched()

    def prepareToAdvancePlaylist(self):
        if self.playlist.canSwitchToNextPlaylistIndex():
            self.ui.showDebugMessage("Preparing to advance playlist...")
            self.lastAdvanceTime = time.time()
        else:
            self.ui.showDebugMessage("Not preparing to advance playlist because the next file cannot be switched to")

    def _recentlyAdvanced(self):
        lastAdvandedDiff = time.time() - self.lastAdvanceTime if self.lastAdvanceTime else None
        if lastAdvandedDiff is not None and lastAdvandedDiff < constants.AUTOPLAY_DELAY + 5:
            return True

    def recentlyConnected(self):
        connectDiff = time.time() - self.lastConnectTime if self.lastConnectTime else None
        if connectDiff is None or connectDiff < constants.LAST_PAUSED_DIFF_THRESHOLD:
            return True

    def recentlyRewound(self, recentRewindThreshold = 5.0):
        lastRewindTime = self.lastRewindTime
        if lastRewindTime and self.lastUpdatedFileTime and self.lastUpdatedFileTime > lastRewindTime:
            lastRewindTime = self.lastRewindTime - 4.5
        return lastRewindTime is not None and abs(time.time() - lastRewindTime) < recentRewindThreshold

    def _pauseChangeIsPlayerNoise(self):
        # A pause change nobody pressed a key for: the seek back to the room position, the first
        # seconds after connecting, and a file that has just finished loading (players start playing
        # of their own accord, which reads as an unpause while the room is paused).
        if self.recentlyRewound() or self.recentlyConnected():
            return True
        return self.lastUpdatedFileTime is not None \
            and time.time() - self.lastUpdatedFileTime < constants.ROOM_LOCK_READY_TOGGLE_GRACE

    def isBuffering(self):
        return self._buffering

    def getBufferCachePercent(self):
        return self._bufferCachePercent

    def bufferHoldIsActive(self):
        return self._bufferHoldActive

    def setBufferHoldActive(self, active):
        self._bufferHoldActive = active

    def _playerBufferState(self):
        """(stalled, cachePercent) straight from the player, or None if it cannot answer.

        None is not "not buffering": it sends detection to the position-stall heuristic, which is
        the only thing every other player can support.
        """
        if self._player is None or not getattr(self._player, "bufferStateSupported", False):
            return None
        return self._player.getBufferState()

    def _positionIsFrozen(self, position):
        """Whether playback has made no real progress for BUFFER_STALL_DETECT.

        The baseline moves whenever the position actually advances, so this is a sliding window
        rather than a one-shot sample - the same "sustained evidence" idea as _desyncSustainedFor,
        but measured against the player's own clock instead of the server's reports. A backwards
        jump is a seek, not a stall, and also resets the baseline.
        """
        now = time.time()
        if self._stallReference is None:
            self._stallReference = (now, position)
            return False
        referenceTime, referencePosition = self._stallReference
        if position < referencePosition or position - referencePosition > constants.BUFFER_STALL_TOLERANCE:
            self._stallReference = (now, position)
            return False
        return now - referenceTime >= constants.BUFFER_STALL_DETECT

    def _bufferHoldIsInForce(self):
        """Whether a buffer hold has the room paused - the server's (for anyone) or our own fallback.

        Somebody else's hold counts: it stops our playback just as thoroughly, so our own stall
        detection cannot see anything either way while it lasts.
        """
        return self._bufferHoldActive or self._bufferFallbackPaused

    def _stallStandsWhileHeld(self):
        """Are we still stalled, asked while a buffer hold has playback stopped?

        This is the one situation where the ordinary rules cannot answer. Detection is built on a
        position that should be advancing and is not - but nothing is advancing during a hold,
        because the hold paused it. Reading that as "recovered" is what made a hold cancel itself
        about a second after it started, over and over, on exactly the connections it exists for.

        A player that tracks its own cache is simply asked. Anything else keeps the verdict it had
        for BUFFER_HOLD_SETTLE - long enough to be worth having paused for, short enough that a
        cache which did fill is not sat on - and the room then tries again. A stall that really is
        not clearing now reaches the server's own patience limit (BUFFER_HOLD_MAX) instead of
        restarting the clock on every cycle.

        A client that was not stalled to begin with (the hold is somebody else's) has no verdict to
        keep and simply stays not-stalled: _bufferingSince is None and _buffering is False.
        """
        # Neither the frozen-position baseline nor the sustained-evidence clock means anything while
        # playback is stopped, and a stale one would let the poll straight after the hold declare a
        # fresh stall with no evidence at all - which is the flap again, one release later.
        self._stallReference = None
        self._clearSustainedDesync("buffering")
        native = self._playerBufferState()
        if native is not None:
            stalled, percent = native
            self._bufferCachePercent = percent if stalled else None
            return stalled
        if self._bufferingSince is None:
            return self._buffering
        return time.time() - self._bufferingSince < constants.BUFFER_HOLD_SETTLE

    def _bufferingEvidence(self, paused, position):
        """Whether there is enough evidence that our player is stalled filling a cache.

        Two sources, one answer. mpv reports `paused-for-cache` itself, which says *why* playback
        stopped; everyone else gets the heuristic, which can only say that it did. Either way the
        room is not told to wait on less than BUFFER_STALL_DETECT of evidence.
        """
        if self._player is None or not self.userlist.currentUser.file:
            return False
        if self._bufferHoldIsInForce():
            return self._stallStandsWhileHeld()
        if paused or self.getGlobalPaused() or self._lastGlobalUpdate is None:
            return False  # nobody is meant to be playing: a still position means nothing
        if self._pauseChangeIsPlayerNoise() or self.waitingToLoadNewfile:
            # A frozen position right after a seek, a file load or a connection is how those look
            # from here - the same windows that make a pause change player noise rather than a
            # keypress. Reset the baseline so the file that is loading does not arrive pre-stalled.
            self._stallReference = None
            self._clearSustainedDesync("buffering")
            return False
        native = self._playerBufferState()
        if native is not None:
            stalled, percent = native
            self._bufferCachePercent = percent if stalled else None
            self._stallReference = None  # the heuristic is not in play; do not carry a stale baseline
            return self._desyncSustainedFor("buffering", stalled, constants.BUFFER_STALL_DETECT)
        if self._atEndOfFile(position) or self._recentlyAdvanced():
            # A player that has run out of file looks exactly like a stalled one to the heuristic:
            # position frozen, still calling itself unpaused. Several players sit on the last frame
            # for a moment before their own pause flag catches up, which is long enough to clear
            # BUFFER_STALL_DETECT and pause the whole room as the file ends. mpv is exempt because
            # its own paused-for-cache flag answers the question properly (handled above).
            self._stallReference = None
            return False
        self._bufferCachePercent = None
        return self._positionIsFrozen(position)

    def _atEndOfFile(self, position):
        currentLength = self.userlist.currentUser.file["duration"] if self.userlist.currentUser.file else 0
        if not currentLength or currentLength <= 0:
            return False  # unknown duration (a live stream): nothing to be at the end of
        return abs(position - currentLength) < constants.PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD

    def _updateBufferingState(self, paused, position):
        if not self._config['pauseOnBuffer']:
            # Opted out: do not even look. The room can still be held for somebody *else's* stall -
            # that arrives from the server and is handled independently of this setting.
            self._buffering = False
            return
        stalled = self._bufferingEvidence(paused, position)
        if stalled and not self._buffering:
            self._buffering = True
            self._bufferingSince = time.time()
            self._clearSustainedDesync("bufferrecover")
            self._onBufferingStarted()
        elif self._buffering and self._desyncSustainedFor("bufferrecover", not stalled, constants.BUFFER_RECOVER_HOLD):
            self._buffering = False
            self._clearSustainedDesync("bufferrecover")
            self._onBufferingFinished()

    def _reportBufferingImmediately(self):
        # The State heartbeat would carry it within a second anyway; sending now shortens the window
        # in which the room is still reacting to a stall it has not been told about. stateChange is
        # deliberately False - this is not a playstate change and must not bump ignoringOnTheFly.
        if self._protocol and self._protocol.logged:
            self._protocol.sendState(self.getPlayerPosition(), self.getPlayerPaused(), False, None, False)

    def _onBufferingStarted(self):
        self.ui.showDebugMessage("Buffering detected (cache {}%)".format(self._bufferCachePercent))
        if self.serverFeatures.get("bufferPause"):
            self._reportBufferingImmediately()
        else:
            self._bufferHoldFallback()

    def _onBufferingFinished(self):
        bufferedFor = time.time() - self._bufferingSince if self._bufferingSince else 0
        self.ui.showDebugMessage("Buffering finished after {:.1f}s".format(bufferedFor))
        self._bufferingSince = None
        self._bufferCachePercent = None
        if self.serverFeatures.get("bufferPause"):
            self._reportBufferingImmediately()
        else:
            self._releaseBufferHoldFallback()

    def _bufferHoldFallback(self):
        """Stand in for the server's buffer hold when it does not have one.

        Only ever pauses when we are entitled to control the room: in a locked or managed room the
        server rejects the pause and converts it into a readiness toggle
        (Watcher._readinessToggleFromRejectedPause), so pressing it here would flip our readiness
        instead of pausing anybody. There we say so in chat and leave it at that.
        """
        self._sendBufferingChatNotice()
        if self.getGlobalPaused() or not self.userlist.currentUser.canControl():
            return
        if not (self._protocol and self._protocol.logged):
            return
        self._bufferFallbackPaused = True
        self.setPaused(True)
        self._protocol.sendState(self.getPlayerPosition(), True, False, None, True)

    def _releaseBufferHoldFallback(self):
        # Only undo our own pause, and only if it is still ours to undo: anybody pausing on purpose
        # in the meantime outranks a cache that has finished filling.
        if not self._bufferFallbackPaused:
            return
        self._bufferFallbackPaused = False
        if not self.getGlobalPaused():
            return
        if not (self._protocol and self._protocol.logged):
            return
        self.setPaused(False)
        self._protocol.sendState(self.getPlayerPosition(), False, False, None, True)

    def _sendBufferingChatNotice(self):
        if not self.serverFeatures.get("chat"):
            return
        now = time.time()
        if self._lastBufferChatTime is not None and now - self._lastBufferChatTime < constants.BUFFER_CHAT_MIN_INTERVAL:
            return  # a flapping link must not turn into a wall of chat
        self._lastBufferChatTime = now
        self.sendChat(getMessage("buffering-local-chat-message"))

    def _toggleReadyInLockedRoom(self):
        """Turn a pause keypress into a readiness toggle while a server admin has the room locked.

        Playback belongs to the admins, so the player goes straight back to the room's state and the
        pause change is never sent (the server would only reject and revert it). Unlike the managed-
        room branch below this toggles in both directions - pressing play in a paused room is exactly
        how you say "I am ready" - so player noise has to be filtered out explicitly instead.
        """
        self._player.setPaused(self._globalPaused)
        self._playerPaused = self._globalPaused
        if self._pauseChangeIsPlayerNoise():
            return False
        if self.userlist.currentUser.isReady():
            self.ui.showMessage(getMessage("set-as-not-ready-notification"))
        else:
            self.ui.showMessage(getMessage("set-as-ready-notification"))
        self.toggleReady(manuallyInitiated=True)
        return False

    def _toggleReady(self, pauseChange, paused):
        if self.userlist.currentUser.isRoomLocked() and not self.userlist.currentUser.isController():
            return self._toggleReadyInLockedRoom()
        if not self.userlist.currentUser.canControl():
            self._player.setPaused(self._globalPaused)
            if not self.recentlyRewound() and not ((self._globalPaused == True) and not self._recentlyAdvanced()):
                self.toggleReady(manuallyInitiated=True)
            self._playerPaused = self._globalPaused
            pauseChange = False
            if self.userlist.currentUser.isReady():
                self.ui.showMessage(getMessage("set-as-not-ready-notification"))
            else:
                self.ui.showMessage(getMessage("set-as-ready-notification"))
        elif self.seamlessMusicOveride():
            self.ui.showDebugMessage("Readiness toggle ignored due to seamless music override")
            self._player.setPaused(paused)
            self._playerPaused = paused
        elif (self.recentlyRewound() and (self._globalPaused == True) and not self._recentlyAdvanced()):
            self._player.setPaused(self._globalPaused)
            self._playerPaused = self._globalPaused
            pauseChange = False
        elif not paused and not self.instaplayConditionsMet():
            paused = True
            self._player.setPaused(paused)
            self._playerPaused = paused
            self.changeReadyState(True, manuallyInitiated=True)
            pauseChange = False
            self.ui.showMessage(getMessage("ready-to-unpause-notification"))
        else:
            lastPausedDiff = time.time() - self.lastPausedOnLeaveTime if self.lastPausedOnLeaveTime else None
            if lastPausedDiff is not None and lastPausedDiff < constants.LAST_PAUSED_DIFF_THRESHOLD:
                self.lastPausedOnLeaveTime = None
            else:
                self.changeReadyState(not self.getPlayerPaused(), manuallyInitiated=False)
        return pauseChange

    def getLocalState(self):
        paused = self.getPlayerPaused()
        if self._config['dontSlowDownWithMe']:
            position = self.getGlobalPosition()
        else:
            position = self.getPlayerPosition()
        pauseChange, _ = self._determinePlayerStateChange(paused, position)
        if self._lastGlobalUpdate:
            return position, paused, _, pauseChange
        else:
            return None, None, None, None

    def _initPlayerState(self, position, paused):
        if self.userlist.currentUser.file:
            self.setPosition(position)
            self._player.setPaused(paused)
            madeChangeOnPlayer = True
            return madeChangeOnPlayer

    def _rewindPlayerDueToTimeDifference(self, position, setBy):
        madeChangeOnPlayer = False
        if self.getUsername() == setBy:
            self.ui.showDebugMessage("Caught attempt to rewind due to time difference with self")
        else:
            hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
            self.setPosition(position)
            self.ui.showMessage(getMessage("rewind-notification").format(setBy), hideFromOSD)
            madeChangeOnPlayer = True
        return madeChangeOnPlayer

    def _fastforwardPlayerDueToTimeDifference(self, position, setBy):
        madeChangeOnPlayer = False
        if self.getUsername() == setBy:
            self.ui.showDebugMessage("Caught attempt to fastforward due to time difference with self")
        else:
            hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
            self.setPosition(position + constants.FASTFORWARD_EXTRA_TIME)
            self.ui.showMessage(getMessage("fastforward-notification").format(setBy), hideFromOSD)
            madeChangeOnPlayer = True
        return madeChangeOnPlayer

    def _serverUnpaused(self, setBy):
        hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
        self._player.setPaused(False)
        madeChangeOnPlayer = True
        self.ui.showMessage(getMessage("unpause-notification").format(setBy), hideFromOSD)
        return madeChangeOnPlayer

    def _serverPaused(self, setBy):
        hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
        if constants.SYNC_ON_PAUSE and self.getUsername() != setBy:
            self.setPosition(self.getGlobalPosition())
        self._player.setPaused(True)
        madeChangeOnPlayer = True
        if (self.lastLeftTime < time.time() - constants.OSD_DURATION) or hideFromOSD == True:
            self.ui.showMessage(getMessage("pause-notification").format(setBy, utils.formatTime(self.getGlobalPosition())), hideFromOSD)
        else:
            self.ui.showMessage(getMessage("left-paused-notification").format(self.lastLeftUser, setBy), hideFromOSD)
        return madeChangeOnPlayer

    def _serverSeeked(self, position, setBy):
        hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
        if self.getUsername() != setBy:
            self.playerPositionBeforeLastSeek = self.getPlayerPosition()
            self.setPosition(position)
            madeChangeOnPlayer = True
        else:
            madeChangeOnPlayer = False
        message = getMessage("seek-notification").format(setBy, utils.formatTime(self.playerPositionBeforeLastSeek), utils.formatTime(position))
        self.ui.showMessage(message, hideFromOSD)
        return madeChangeOnPlayer

    def _desyncSustainedFor(self, key, active, duration):
        """Whether a desync condition has held continuously for `duration`.

        One State message is not evidence. The position it carries is corrected by a latency
        estimate, and on an unstable link that estimate moves around, so acting on a single sample
        turns link jitter into visible seeks and speed changes. This mirrors the sustained-evidence
        pattern the fast-forward path has always used via `behindFirstDetected`.
        """
        if not active:
            self._desyncSince[key] = None
            return False
        if self._desyncSince.get(key) is None:
            self._desyncSince[key] = time.time()
            return False
        return time.time() - self._desyncSince[key] >= duration

    def _clearSustainedDesync(self, key):
        self._desyncSince[key] = None

    def _slowDownToCoverTimeDifference(self, diff, setBy):
        hideFromOSD = not constants.SHOW_SLOWDOWN_OSD
        madeChangeOnPlayer = False
        wantsSlowdown = self._config['slowdownThreshold'] < diff and not self._speedChanged
        if self._desyncSustainedFor("slowdown", wantsSlowdown, constants.SLOWDOWN_SUSTAIN_DURATION):
            if self.getUsername() == setBy:
                self.ui.showDebugMessage("Caught attempt to slow down due to time difference with self")
            else:
                self._player.setSpeed(constants.SLOWDOWN_RATE)
                self._speedChanged = True
                self.ui.showMessage(getMessage("slowdown-notification").format(setBy), hideFromOSD)
                madeChangeOnPlayer = True
        elif self._speedChanged and diff < constants.SLOWDOWN_RESET_THRESHOLD:
            self._player.setSpeed(1.00)
            self._speedChanged = False
            self.ui.showMessage(getMessage("revert-notification"), hideFromOSD)
            madeChangeOnPlayer = True
        return madeChangeOnPlayer

    def _desyncReactionsAreMeaningless(self):
        """Whether the measured time difference is a cache stall rather than a desync.

        A stalled player is behind because it has nothing to play, and a room being held for
        somebody else's stall is about to stop anyway. Seeking or changing speed in either case
        corrects nothing and is exactly the juddering this feature exists to remove.
        """
        return self._buffering or self._bufferHoldActive

    def _standDownFromDesyncReactions(self):
        # Drop the evidence as well as the reaction: a stall that outlasts REWIND_SUSTAIN_DURATION
        # would otherwise fire a rewind the moment it clears. Speed goes back immediately rather
        # than waiting for the difference to fall under SLOWDOWN_RESET_THRESHOLD, which it cannot
        # do while the player is not playing.
        self._clearSustainedDesync("rewind")
        self._clearSustainedDesync("slowdown")
        self.behindFirstDetected = None
        if self._speedChanged:
            self._player.setSpeed(1.00)
            self._speedChanged = False
            self.ui.showMessage(getMessage("revert-notification"), not constants.SHOW_SLOWDOWN_OSD)
            return True
        return False

    def _changePlayerStateAccordingToGlobalState(self, position, paused, doSeek, setBy):
        madeChangeOnPlayer = False
        pauseChanged = paused != self.getGlobalPaused() or paused != self.getPlayerPaused()
        diff = self.getPlayerPosition() - position
        if self._lastGlobalUpdate is None:
            madeChangeOnPlayer = self._initPlayerState(position, paused)
        self._globalPaused = paused
        self._globalPosition = position
        self._lastGlobalUpdate = time.time()
        if doSeek:
            madeChangeOnPlayer = self._serverSeeked(position, setBy)
        if self._desyncReactionsAreMeaningless():
            madeChangeOnPlayer = self._standDownFromDesyncReactions() or madeChangeOnPlayer
        else:
            rewindWanted = diff > self._config['rewindThreshold'] and not doSeek and not self._config['rewindOnDesync'] == False
            if self._desyncSustainedFor("rewind", rewindWanted, constants.REWIND_SUSTAIN_DURATION):
                madeChangeOnPlayer = self._rewindPlayerDueToTimeDifference(position, setBy)
                self._clearSustainedDesync("rewind")
            if self._config['fastforwardOnDesync'] and (self.userlist.currentUser.canControl() == False or self._config['dontSlowDownWithMe'] == True):
                if diff < (constants.FASTFORWARD_BEHIND_THRESHOLD * -1) and not doSeek:
                    if self.behindFirstDetected is None:
                        self.behindFirstDetected = time.time()
                    else:
                        durationBehind = time.time() - self.behindFirstDetected
                        if (durationBehind > (self._config['fastforwardThreshold']-constants.FASTFORWARD_BEHIND_THRESHOLD))\
                                and (diff < (self._config['fastforwardThreshold'] * -1)):
                            madeChangeOnPlayer = self._fastforwardPlayerDueToTimeDifference(position, setBy)
                            self.behindFirstDetected = time.time() + constants.FASTFORWARD_RESET_THRESHOLD
                else:
                    self.behindFirstDetected = None
            if self._player.speedSupported and not doSeek and not paused and  not self._config['slowOnDesync'] == False:
                madeChangeOnPlayer = self._slowDownToCoverTimeDifference(diff, setBy)
        if paused == False and pauseChanged:
            madeChangeOnPlayer = self._serverUnpaused(setBy)
        elif paused == True and pauseChanged:
            madeChangeOnPlayer = self._serverPaused(setBy)
        return madeChangeOnPlayer

    def _executePlaystateHooks(self, position, paused, doSeek, setBy, messageAge):
        if self.userlist.hasRoomStateChanged() and not paused:
            self._warnings.checkWarnings()
            self.userlist.roomStateConfirmed()

    def updateGlobalState(self, position, paused, doSeek, setBy, messageAge):
        if self.__getUserlistOnLogon:
            self.__getUserlistOnLogon = False
            self.getUserList()
        madeChangeOnPlayer = False
        if not paused:
            position += min(messageAge, constants.MAX_MESSAGE_AGE)
        if self._player:
            madeChangeOnPlayer = self._changePlayerStateAccordingToGlobalState(position, paused, doSeek, setBy)
        if madeChangeOnPlayer:
            self.askPlayer()
        self._executePlaystateHooks(position, paused, doSeek, setBy, messageAge)

    def getUserOffset(self):
        return self._userOffset

    def setUserOffset(self, time):
        self._userOffset = time
        self.setPosition(self.getGlobalPosition())
        self.ui.showMessage(getMessage("current-offset-notification").format(self._userOffset))

    def onDisconnect(self):
        if self._config['pauseOnLeave']:
            self.setPaused(True)
            self.lastPausedOnLeaveTime = time.time()

    def removeUser(self, username):
        if self.userlist.isUserInYourRoom(username):
            self.onDisconnect()
        self.userlist.removeUser(username)

    def getPlayerPosition(self):
        if not self._lastPlayerUpdate:
            if self._lastGlobalUpdate:
                return self.getGlobalPosition()
            else:
                return 0.0
        position = self._playerPosition
        if not self._playerPaused:
            diff = time.time() - self._lastPlayerUpdate
            position += diff
        return position

    def getStoredPlayerPosition(self):
        return self._playerPosition if self._playerPosition is not None else None

    def getPlayerPaused(self):
        if not self._lastPlayerUpdate:
            if self._lastGlobalUpdate:
                return self.getGlobalPaused()
            else:
                return True
        return self._playerPaused

    def getGlobalPosition(self):
        if not self._lastGlobalUpdate:
            return 0.0
        position = self._globalPosition
        if not self._globalPaused:
            position += time.time() - self._lastGlobalUpdate
        return position

    def getGlobalPaused(self):
        if not self._lastGlobalUpdate:
            return True
        return self._globalPaused

    def eofReportedByPlayer(self):
        if self.playlist.notJustChangedPlaylist() and self.userlist.currentUser.file:
            self.ui.showDebugMessage("Fixing file duration to allow for playlist advancement")
            self.userlist.currentUser.file["duration"] = self._playerPosition

    def _syncNewlyLoadedFileToRoom(self):
        """Seek a file that has just finished loading to where the room already is.

        Without this, a client that loads its file after joining sits at 00:00 while everyone else
        is mid-playback - and since a server takes the *least* advanced watcher as the room
        position, it drags the whole room back to the start with it. This fork's server pulls
        newcomers into sync by itself, but stock servers do not, so do it locally too.

        Only ever applies inside the join window (until our player has genuinely been in sync
        once), so deliberately switching to another file mid-session is left alone.
        """
        suppressed, self._suppressSyncOnNextFileLoad = self._suppressSyncOnNextFileLoad, False
        if suppressed or self._syncedWithRoomSinceConnect:
            return
        if not self._lastGlobalUpdate or not self._player or not self.userlist.currentUser.file:
            return
        globalPosition = self.getGlobalPosition()
        if globalPosition - self.getPlayerPosition() > constants.CLIENT_SYNC_ON_FILE_LOAD_THRESHOLD:
            self.ui.showDebugMessage("Seeking newly loaded file to the room position ({})".format(globalPosition))
            self.setPosition(globalPosition)

    def updateFile(self, filename, duration, path):
        self.lastUpdatedFileTime = time.time()
        newPath = ""
        if utils.isURL(path):
            filename = path
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except:
            try:
                path = path.decode('utf-8')
                size = os.path.getsize(path)
            except:
                size = 0
        if not utils.isURL(path) and os.path.exists(path):
            self.fileSwitch.notifyUserIfFileNotInMediaDirectory(filename, path)
        filename, size = self.__executePrivacySettings(filename, size)
        self.userlist.currentUser.setFile(filename, duration, size, path)
        self.sendFile()
        self._syncNewlyLoadedFileToRoom()
        self.playlist.changeToPlaylistIndexFromFilename(filename)
        self.playlist.doubleCheckForWatchedPreviousFile()

    def setTrustedDomains(self, newTrustedDomains):
        from syncplay.ui.ConfigurationGetter import ConfigurationGetter
        ConfigurationGetter().setConfigOption("trustedDomains", newTrustedDomains)
        oldTrustedDomains = self._config['trustedDomains']
        if oldTrustedDomains != newTrustedDomains:
            self._config['trustedDomains'] = newTrustedDomains
            self.fileSwitchFoundFiles()
            self.ui.showMessage("Trusted domains updated")
            # TODO: Properly add message for setting trusted domains!
            # TODO: Handle cases where users add www. to start of domain
        # Auto-share to the room when the session flag is on. Runs even if the list is unchanged so
        # ticking "share" in the dialog on an unchanged list still publishes; gated on admin/controller
        # status to avoid firing a server-rejected publish if admin was lost mid-session.
        if self._shareTrustedDomainsOnUpdate and self.userlist.currentUser.isController():
            self.publishTrustedDomains()

    def getShareTrustedDomainsOnUpdate(self):
        return self._shareTrustedDomainsOnUpdate

    def setShareTrustedDomainsOnUpdate(self, enabled):
        self._shareTrustedDomainsOnUpdate = bool(enabled)

    def effectiveTrustedDomains(self):
        # User's own trusted domains, plus any accepted server-published ones (session-only overlay).
        # Order-preserving union so the user's list always wins first; opt-out disables the overlay.
        domains = list(self._config['trustedDomains']) if self._config['trustedDomains'] else []
        if self._config.get('receiveServerTrustedDomains', True) and self._serverTrustedDomains:
            for entry in self._serverTrustedDomains:
                if entry not in domains:
                    domains.append(entry)
        return domains

    def publishTrustedDomains(self):
        # Admin action: publish this client's own trusted-domains list to the room. The server
        # enforces admin authorization; a non-admin gets a private error back as chat.
        if self._protocol and self._protocol.logged:
            domains = self._config['trustedDomains'] if self._config['trustedDomains'] else []
            self._protocol.sendTrustedDomains({"domains": domains, "by": self.getUsername()})

    def setServerTrustedDomains(self, values):
        # Received Set:trustedDomains. Store session-only; merged in effectiveTrustedDomains() when
        # the user has not opted out. The raw list is always kept so re-enabling the opt-in
        # mid-session takes effect without a reconnect.
        if not isinstance(values, dict):
            return
        rawDomains = values.get("domains")
        if not isinstance(rawDomains, list):
            return
        domains = []
        for entry in rawDomains:
            if isinstance(entry, str) and entry.strip():
                cleaned = entry.strip().lower()[:constants.TRUSTED_DOMAINS_MAX_LENGTH]
                if cleaned not in domains:
                    domains.append(cleaned)
            if len(domains) >= constants.TRUSTED_DOMAINS_MAX_COUNT:
                break
        self._serverTrustedDomains = domains
        if domains and self._config.get('receiveServerTrustedDomains', True):
            self.fileSwitchFoundFiles()  # re-evaluate pending file-switch trust with the new domains
            self.ui.showMessage(getMessage("server-trusted-domains-notification").format(
                len(domains), values.get("by", "")))

    def setRoomList(self, newRoomList):
        newRoomList = sorted(newRoomList)
        from syncplay.ui.ConfigurationGetter import ConfigurationGetter
        ConfigurationGetter().setConfigOption("roomList", newRoomList)
        oldRoomList = self._config['roomList']
        if oldRoomList != newRoomList:
            self._config['roomList'] = newRoomList

    def _isURITrustableAndTrusted(self, URIToTest):
        """Returns a tuple of booleans: (trustable, trusted).

        A given URI is "trustable" if it uses HTTP or HTTPS (constants.TRUSTABLE_WEB_PROTOCOLS).
        A given URI is "trusted" if it matches an entry in the trustedDomains config.
        Such an entry is considered matching if the domain is the same and the path
        is a prefix of the given URI's path.
        A "trustable" URI is always "trusted" if the config onlySwitchToTrustedDomains is false.
        """
        try:
            o = urlparse(URIToTest)
            hostname = o.hostname  # raises ValueError on a malformed IPv6 literal/port
        except ValueError:
            # not parseable as a URL, so it can never be trustable
            return False, False
        trustable = o.scheme in constants.TRUSTABLE_WEB_PROTOCOLS
        if not trustable:
            # untrustable URIs are never trusted, return early
            return False, False
        if not self._config['onlySwitchToTrustedDomains']:
            # trust all trustable URIs in this case
            return trustable, True
        # check for matching trusted domains (user's list plus any accepted server-published ones)
        effectiveTrustedDomains = self.effectiveTrustedDomains()
        if effectiveTrustedDomains:
            for entry in effectiveTrustedDomains:
                trustedDomain, _, path = entry.partition('/')
                foundMatch = False
                if hostname in (trustedDomain, "www." + trustedDomain):
                    foundMatch = True
                elif "*" in trustedDomain and hostname is not None:
                    wildcardRegex = "^("+re.escape(trustedDomain).replace("\\*","([^.]+)")+")$"
                    wildcardMatch = bool(re.fullmatch(wildcardRegex, hostname, re.IGNORECASE))
                    if wildcardMatch:
                        foundMatch = True
                if not foundMatch:
                    continue
                if path and not o.path.startswith('/' + path):
                    # trusted domain has a path component and it does not match
                    continue
                # match found, trust this domain
                return trustable, True
        # no matches found, do not trust this domain
        return trustable, False

    def isUntrustedTrustableURI(self, URIToTest):
        if utils.isURL(URIToTest):
            trustable, trusted = self._isURITrustableAndTrusted(URIToTest)
            return trustable and not trusted
        return False

    def isURITrusted(self, URIToTest):
        trustable, trusted = self._isURITrustableAndTrusted(URIToTest)
        return trustable and trusted

    def openFile(self, filePath, resetPosition=False, fromUser=False):
        if not (filePath.startswith("http://") or filePath.startswith("https://"))\
                and ((fromUser and filePath.endswith(".txt")) or filePath.endswith(".m3u") or filePath.endswith(".m3u8")):
            self.playlist.loadPlaylistFromFile(filePath, resetPosition)
            return

        self.playlist.openedFile()
        if resetPosition:
            self._suppressSyncOnNextFileLoad = True  # starting this file from the beginning is the point
        self._player.openFile(filePath, resetPosition)
        if resetPosition:
            self.rewindFile()
            self.establishRewindDoubleCheck()
            self.lastRewindTime = time.time()
            self.autoplayCheck()
        self.playlist.doubleCheckForWatchedPreviousFile()

    def fileSwitchFoundFiles(self):
        self.ui.fileSwitchFoundFiles()
        self.playlist.loadCurrentPlaylistIndex()

    def setPlaylistIndex(self, index):
        self._protocol.setPlaylistIndex(index)
        self.playlist.doubleCheckForWatchedPreviousFile()

    def changeToPlaylistIndex(self, *args, **kwargs):
        self.playlist.changeToPlaylistIndex(*args, **kwargs)
        self.playlist.doubleCheckForWatchedPreviousFile()

    def loopSingleFiles(self):
        return self._config["loopSingleFiles"] or self.isPlayingMusic()

    def isPlaylistLoopingEnabled(self):
        return self._config["loopAtEndOfPlaylist"] or self.isPlayingMusic()

    def __executePrivacySettings(self, filename, size):
        if self._config['filenamePrivacyMode'] == PRIVACY_SENDHASHED_MODE:
            filename = utils.hashFilename(filename)
        elif self._config['filenamePrivacyMode'] == PRIVACY_DONTSEND_MODE:
            filename = PRIVACY_HIDDENFILENAME
        if self._config['filesizePrivacyMode'] == PRIVACY_SENDHASHED_MODE:
            size = utils.hashFilesize(size)
        elif self._config['filesizePrivacyMode'] == PRIVACY_DONTSEND_MODE:
            size = 0
        return filename, size

    def setServerVersion(self, version, featureList):
        self.serverVersion = version
        self.checkForFeatureSupport(featureList)
        self._autoAuthAdmin()

    def requestTrackPublish(self):
        # /tracks command or hotkey: ask the player to read its current track selection and
        # publish it (comes back via publishTrackProposal). Admin authorization is server-side.
        if self._player and getattr(self._player, "trackProposalsSupported", False):
            self._player.requestTrackPublish()
        else:
            self.ui.showErrorMessage(getMessage("tracks-not-supported-by-player-error"))

    def publishTrackProposal(self, payload):
        if self._protocol and self._protocol.logged and isinstance(payload, dict):
            self._protocol.sendTrackProposal(payload)

    def _autoAuthAdmin(self):
        # Auto-authenticate as server admin when a password is configured. Sent as a dedicated
        # Set:adminAuth message (never as chat) so servers without the feature ignore it silently
        # and the password can never leak into room chat. Runs per connection (admin status does
        # not survive reconnects server-side).
        adminPassword = self._config.get("adminPassword")
        if adminPassword and self.serverFeatures.get("serverAdmin") and self._protocol:
            self._protocol.sendAdminAuth(adminPassword)

    def sendFeaturesToPlayer(self):
        self._player.setFeatures(self.serverFeatures)

    def checkForFeatureSupport(self, featureList):
        # NB: do not reset _serverTrustedDomains here - this runs on Hello, which the server sends
        # *after* the join-time Set:trustedDomains. initProtocol owns that reset.
        self.serverFeatures = {
            "featureList": utils.meetsMinVersion(self.serverVersion, constants.FEATURE_LIST_MIN_VERSION),
            "sharedPlaylists": utils.meetsMinVersion(self.serverVersion, constants.SHARED_PLAYLIST_MIN_VERSION),
            "chat": utils.meetsMinVersion(self.serverVersion, constants.CHAT_MIN_VERSION),
            "readiness": utils.meetsMinVersion(self.serverVersion, constants.USER_READY_MIN_VERSION),
            "managedRooms": utils.meetsMinVersion(self.serverVersion, constants.CONTROLLED_ROOMS_MIN_VERSION),
            "persistentRooms": False,
            "maxChatMessageLength": constants.FALLBACK_MAX_CHAT_MESSAGE_LENGTH,
            "maxUsernameLength": constants.FALLBACK_MAX_USERNAME_LENGTH,
            "maxRoomNameLength": constants.FALLBACK_MAX_ROOM_NAME_LENGTH,
            "maxFilenameLength": constants.FALLBACK_MAX_FILENAME_LENGTH,
            "setOthersReadiness": utils.meetsMinVersion(self.serverVersion, constants.SET_OTHERS_READINESS_MIN_VERSION),
            "afk": False,  # fork feature; overwritten by the server's featureList when supported
            "setOthersAfk": False,  # fork feature; separate flag so a targeted set can never
                                    # misfire as a self-toggle on an older fork server
            "bufferPause": False  # fork feature; without it we fall back to pausing ourselves
        }
        if featureList:
            self.serverFeatures.update(featureList)
        if not utils.meetsMinVersion(self.serverVersion, constants.SHARED_PLAYLIST_MIN_VERSION):
            self.ui.showErrorMessage(getMessage("shared-playlists-not-supported-by-server-error").format(constants.SHARED_PLAYLIST_MIN_VERSION, self.serverVersion))
        elif not self.serverFeatures["sharedPlaylists"]:
            self.ui.showErrorMessage(getMessage("shared-playlists-disabled-by-server-error"))
        # TODO: Have messages for all unsupported & disabled features
        if self.serverFeatures["maxChatMessageLength"] is not None:
            constants.MAX_CHAT_MESSAGE_LENGTH = self.serverFeatures["maxChatMessageLength"]
        if self.serverFeatures["maxUsernameLength"] is not None:
            constants.MAX_USERNAME_LENGTH = self.serverFeatures["maxUsernameLength"]
        if self.serverFeatures["maxRoomNameLength"] is not None:
            constants.MAX_ROOM_NAME_LENGTH = self.serverFeatures["maxRoomNameLength"]
        if self.serverFeatures["maxFilenameLength"] is not None:
            constants.MAX_FILENAME_LENGTH = self.serverFeatures["maxFilenameLength"]
        constants.MPV_SYNCPLAYINTF_CONSTANTS_TO_SEND = [
            "MaxChatMessageLength={}".format(constants.MAX_CHAT_MESSAGE_LENGTH),
            "inputPromptStartCharacter={}".format(constants.MPV_INPUT_PROMPT_START_CHARACTER),
            "inputPromptEndCharacter={}".format(constants.MPV_INPUT_PROMPT_END_CHARACTER),
            "backslashSubstituteCharacter={}".format(constants.MPV_INPUT_BACKSLASH_SUBSTITUTE_CHARACTER)]
        self.ui.setFeatures(self.serverFeatures)
        if self._player:
            self.sendFeaturesToPlayer()
        else:
            # Player might not have been loaded if connecting to localhost (#545)
            self.addPlayerReadyCallback(lambda x: self.sendFeaturesToPlayer())

    def getSanitizedCurrentUserFile(self):
        if self.userlist.currentUser.file:
            file_ = deepcopy(self.userlist.currentUser.file)
            if constants.PRIVATE_FILE_FIELDS:
                for PrivateField in constants.PRIVATE_FILE_FIELDS:
                    if PrivateField in file_:
                        file_.pop(PrivateField)
            return file_
        else:
            return None

    def sendFile(self):
        file_ = self.getSanitizedCurrentUserFile()
        if self._protocol and self._protocol.logged and file_:
            self._protocol.sendFileSetting(file_)

    def setUsername(self, username):
        if username and username != "":
            self.userlist.currentUser.username = username
        else:
            random_number = random.randrange(1000, 9999)
            self.userlist.currentUser.username = "Anonymous" + str(random_number)  # Not localised as this would give away locale

    def getUsername(self):
        return self.userlist.currentUser.username

    def chatIsEnabled(self):
        return True
        # TODO: Allow chat to be disabled

    def getFeatures(self):
        features = dict()

        # Can change during runtime:
        features["sharedPlaylists"] = self.sharedPlaylistIsEnabled()  # Can change during runtime
        features["chat"] = self.chatIsEnabled()  # Can change during runtime
        features["uiMode"] = self.ui.getUIMode()

        # Static for this version/release of Syncplay:
        features["featureList"] = True
        features["readiness"] = True
        features["managedRooms"] = True
        features["persistentRooms"] = True
        features["setOthersReadiness"] = True
        features["yapTimer"] = self._yapTimerOSDSupported  # Can render the live yap-timer overlay
        features["pauseWarning"] = self._pauseWarningOSDSupported  # Can render the blinking pause-warning OSD
        features["osdMessages"] = self._genericOSDSupported  # Can render generic styled/ASS OSD messages
        features["trackProposals"] = self._trackProposalsSupported  # Can apply admin track proposals
        features["trustedDomains"] = True  # Can receive admin-published trusted domains (player-agnostic)
        features["afk"] = True  # Understands the AFK state channel (player-agnostic)
        features["roomLock"] = True  # Tracks admin room locks itself (player-agnostic)
        # Understands the buffer-hold channel (player-agnostic: the stall heuristic works
        # everywhere, mpv just answers more precisely). Unconditionally true even when
        # pauseOnBuffer is off - that setting stops us reporting our own stalls, it does not stop
        # us being told the room is waiting for somebody else's.
        features["bufferPause"] = True

        return features

    def setRoom(self, roomName, resetAutoplay=False):
        self.lastSetRoomTime = time.time()
        roomSplit = roomName.split(":")
        if roomName.startswith("+") and len(roomSplit) > 2:
            roomName = roomSplit[0] + ":" + roomSplit[1]
            password = roomSplit[2]
            self.storeControlPassword(roomName, password)
            self.ui.updateRoomName(roomName)
        self.userlist.currentUser.room = roomName
        self.userlist.currentUser.setRoomLocked(self.userlist.isRoomLocked(roomName))  # a stale lock does not follow you into another room
        if resetAutoplay:
            self.resetAutoPlayState()

    def sendRoom(self):
        room = self.userlist.currentUser.room
        if self._protocol and self._protocol.logged and room:
            self._protocol.sendRoomSetting(room)
            self.getUserList()
        self.reIdentifyAsController()

    def reIdentifyAsController(self):
        self.setRoom(self.userlist.currentUser.room)
        room = self.userlist.currentUser.room
        if utils.RoomPasswordProvider.isControlledRoom(room):
            storedRoomPassword = self.getControlledRoomPassword(room)
            if storedRoomPassword:
                self.identifyAsController(storedRoomPassword)

    def isConnectedAndInARoom(self):
        return self._protocol and self._protocol.logged and self.userlist.currentUser.room

    def sharedPlaylistIsEnabled(self):
        if "sharedPlaylists" in self.serverFeatures and not self.serverFeatures["sharedPlaylists"]:
            sharedPlaylistEnabled = False
        else:
            sharedPlaylistEnabled = self._config['sharedPlaylistEnabled']
        return sharedPlaylistEnabled

    def connected(self):
        self.lastConnectTime = time.time()
        self._syncedWithRoomSinceConnect = False  # reopens the join window: we may be nowhere near the room
        self.userlist.clearRoomLocks()  # session-only state; the server re-sends it after the Hello
        readyState = self._config['readyAtStart'] if self.userlist.currentUser.isReady() is None else self.userlist.currentUser.isReady()
        self._protocol.setReady(readyState, manuallyInitiated=False)
        self.reIdentifyAsController()
        if self._config["loadPlaylistFromFile"]:
            self.playlist.loadPlaylistFromFile(self._config["loadPlaylistFromFile"])
            self._config["loadPlaylistFromFile"] = None

    def getRoom(self):
        return self.userlist.currentUser.room

    def getConfig(self):
        return self._config

    def getUserList(self):
        if self._protocol and self._protocol.logged:
            self._protocol.sendList()

    def showUserList(self, altUI=None):
        self.userlist.showUserList(altUI)

    def getPassword(self):
        if self.thisIsPublicServer():
            return ""
        else:
            return self._serverPassword

    def thisIsPublicServer(self):
        self._publicServers = []
        if self._publicServers and self._host in self._publicServers:
            return True
        i = 0
        for server in constants.FALLBACK_PUBLIC_SYNCPLAY_SERVERS:
            if server[1] == self._host:
                return True
            i += 1

    def setPosition(self, position):
        if self._lastPlayerUpdate:
            self._lastPlayerUpdate = time.time()
        if self.lastRewindTime is not None and abs(time.time() - self.lastRewindTime) < 1.0 and position > 5:
            self.ui.showDebugMessage("Ignored seek to {} after rewind".format(position))
            return
        # Any seek we command - the sync logic's rewind, a server seek, a user offset change -
        # invalidates the stall baseline: a player that takes a moment to land on the new position
        # reports the old one meanwhile, which reads as frozen. Only openFile's rewind sets
        # lastRewindTime, so _pauseChangeIsPlayerNoise does not cover these.
        self._stallReference = None
        position += self.getUserOffset()
        if self._player and self.userlist.currentUser.file:
            if position < 0:
                position = 0
                self._protocol.sendState(self.getPlayerPosition(), self.getPlayerPaused(), True, None, True)
            self._player.setPosition(position)

    def setPaused(self, paused):
        if self._player and self.userlist.currentUser.file:
            if self._lastPlayerUpdate and not paused:
                self._lastPlayerUpdate = time.time()
            self._player.setPaused(paused)

    def start(self, host, port):
        if self._running:
            return
        self._running = True
        reactor.callLater(constants.UPDATE_STARTUP_OK_DELAY, self._markOverlayStartupSuccessful)
        if self._playerClass:
            perPlayerArguments = utils.getPlayerArgumentsByPathAsArray(self._config['perPlayerArguments'], self._config['playerPath'])
            if perPlayerArguments:
                self._config['playerArgs'].extend(perPlayerArguments)
            filePath = self._config['file']
            if self._config['sharedPlaylistEnabled'] and filePath is not None:
                self.delayedLoadPath = filePath
                filePath = ""
            reactor.callLater(0.1, self._playerClass.run, self, self._config['playerPath'], filePath, self._config['playerArgs'], )
            self._playerClass = None
        self.protocolFactory = SyncClientFactory(self)
        if '[' in host:
            host = host.strip('[]')
        port = int(port)
        self._endpoint = HostnameEndpoint(reactor, host, port)
        try:
            certs = pem.parse_file(SSL_CERT_FILE)
            trustRoot = trustRootFromCertificates([Certificate.loadPEM(str(cert)) for cert in certs])
            self.protocolFactory.options = optionsForClientTLS(hostname=host, trustRoot=trustRoot)
            self._clientSupportsTLS = True
        except Exception as e:
            self.ui.showDebugMessage(str(e))
            self.protocolFactory.options = None
            self._clientSupportsTLS = False

        def retry(retries):
            # Use shared state reset method
            self._performRetryStateReset()
            if retries == 0:
                self.onDisconnect()
            if retries > constants.RECONNECT_RETRIES:
                reactor.callLater(0.1, self.ui.showErrorMessage, getMessage("connection-failed-notification"),
                                  True)
                reactor.callLater(0.1, self.stop, True)
                return None

            return(0.1 * (2 ** min(retries, 5)))

        self._reconnectingService = ClientService(self._endpoint, self.protocolFactory, retryPolicy=retry)
        try:
            waitForConnection = self._reconnectingService.whenConnected(failAfterFailures=1)
        except TypeError:
            waitForConnection = self._reconnectingService.whenConnected()
        self._reconnectingService.startService()

        def connectedNow(f):
            hostIP = connectionHandle.result.transport.addr[0]
            self.ui.showMessage(getMessage("reachout-successful-notification").format(host, hostIP))
            return

        def failed(f):
            reactor.callLater(0.1, self.ui.showErrorMessage, getMessage("connection-failed-notification"), True)
            reactor.callLater(0.1, self.stop, True)

        connectionHandle = waitForConnection.addCallbacks(connectedNow, failed)
        message = getMessage("connection-attempt-notification").format(host, port)
        self.ui.showMessage(message)
        reactor.run()

    def stop(self, promptForAction=False):
        if not self._running:
            return
        self._running = False
        self.destroyProtocol()
        if self._player:
            self._player.drop()

        self.watched.flushQueueOnShutdown()
        if self.ui:
            self.ui.drop()
        reactor.callLater(0.1, reactor.stop)
        if promptForAction:
            self.ui.promptFor(getMessage("enter-to-exit-prompt"))

    def _performRetryStateReset(self):
        """
        Shared method to reset connection state for both automatic and manual retries.
        This contains the common logic from the original retry function.
        """
        self._lastGlobalUpdate = None
        self._syncedWithRoomSinceConnect = False
        self.ui.setSSLMode(False)
        self.playlistMayNeedRestoring = True
        self.ui.showMessage(getMessage("reconnection-attempt-notification"))
        self.reconnecting = True

    def manualReconnect(self):
        """
        Trigger a manual reconnection by forcing the retry mechanism.
        This performs the same steps as the automatic retry function.
        """
        if not self._running or not hasattr(self, '_reconnectingService'):
            self.ui.showErrorMessage(getMessage("connection-failed-notification"))
            return

        from twisted.internet import reactor

        def performReconnect():
            # Apply the shared state reset logic
            self._performRetryStateReset()

            # Stop current service and restart it to trigger reconnection
            if self._reconnectingService and self._reconnectingService.running:
                self._reconnectingService.stopService()

            # Restart the service to trigger a reconnection attempt
            self._reconnectingService.startService()

        # Use callLater for threading purposes as suggested
        reactor.callLater(0.1, performReconnect)

    def requireServerFeature(featureRequired):
        def requireServerFeatureDecorator(f):
            @wraps(f)
            def wrapper(self, *args, **kwds):
                if self.serverVersion == "0.0.0":
                    self.ui.showDebugMessage(
                        "Tried to check server version too soon (testing support for: {})".format(featureRequired))
                    return None
                if featureRequired not in self.serverFeatures or not self.serverFeatures[featureRequired]:
                    featureName = getMessage("feature-{}".format(featureRequired))
                    self.ui.showErrorMessage(getMessage("not-supported-by-server-error").format(featureName))
                    return
                return f(self, *args, **kwds)
            return wrapper
        return requireServerFeatureDecorator

    @requireServerFeature("chat")
    def sendChat(self, message):
        if self._protocol and self._protocol.logged:
            try:
                message = message.replace("\n", "").replace("\r", "")
            except:
                pass
            message = utils.truncateText(message, constants.MAX_CHAT_MESSAGE_LENGTH)
            self._protocol.sendChatMessage(message)

    @requireServerFeature("setOthersReadiness")
    def setOthersReadiness(self, username, newReadyStatus):
        self._protocol.setReady(newReadyStatus, True, username)

    def sendFeaturesUpdate(self, features):
        self._protocol.sendFeaturesUpdate(features)

    def changePlaylistEnabledState(self, newState):
        oldState = self.sharedPlaylistIsEnabled()
        from syncplay.ui.ConfigurationGetter import ConfigurationGetter
        ConfigurationGetter().setConfigOption("sharedPlaylistEnabled", newState)
        self._config["sharedPlaylistEnabled"] = newState
        if oldState == False and newState == True:
            self.playlist.loadCurrentPlaylistIndex()

    def changeAutoplayState(self, newState):
        self.autoPlay = newState
        self.autoplayCheck()

    def changeAutoPlayThrehsold(self, newThreshold):
        oldAutoplayConditionsMet = self.autoplayConditionsMet()
        self.autoPlayThreshold = newThreshold
        newAutoplayConditionsMet = self.autoplayConditionsMet()
        if oldAutoplayConditionsMet == False and newAutoplayConditionsMet == True:
            self.autoplayCheck()

    def autoplayCheck(self):
        if self.isPlayingMusic():
            return True
        if self.autoplayConditionsMet():
            self.startAutoplayCountdown()
        else:
            self.stopAutoplayCountdown()

    def instaplayConditionsMet(self):
        if self.isPlayingMusic():
            return True
        if not self.userlist.currentUser.canControl():
            return False

        unpauseAction = self._config['unpauseAction']
        if self.userlist.currentUser.isReady() or unpauseAction == constants.UNPAUSE_ALWAYS_MODE:
            return True
        elif unpauseAction == constants.UNPAUSE_IFOTHERSREADY_MODE and self.userlist.areAllOtherUsersInRoomReady():
            return True
        elif unpauseAction == constants.UNPAUSE_IFMINUSERSREADY_MODE and self.userlist.areAllOtherUsersInRoomReady()\
                and self.autoPlayThreshold and self.userlist.usersInRoomCount() >= self.autoPlayThreshold:
            return True
        else:
            return False

    def autoplayConditionsMet(self):
        if self.seamlessMusicOveride():
            self.setPaused(False)
        recentlyAdvanced = self._recentlyAdvanced()
        return (
            self._playerPaused and (self.autoPlay or recentlyAdvanced) and
            self.userlist.currentUser.canControl() and self.userlist.isReadinessSupported()
            and self.userlist.areAllUsersInRoomReady(requireSameFilenames=self._config["autoplayRequireSameFilenames"])
            and ((self.autoPlayThreshold and self.userlist.usersInRoomCount() >= self.autoPlayThreshold) or recentlyAdvanced)
        )

    def autoplayTimerIsRunning(self):
        return self.autoplayTimer.running

    def startAutoplayCountdown(self):
        if self.autoplayConditionsMet() and not self.autoplayTimer.running:
            self.autoplayTimeLeft = constants.AUTOPLAY_DELAY
            self.autoplayTimer.start(1)

    def stopAutoplayCountdown(self):
        if self.autoplayTimer.running:
            self.autoplayTimer.stop()
        self.autoplayTimeLeft = constants.AUTOPLAY_DELAY

    def autoplayCountdown(self):
        if not self.autoplayConditionsMet():
            self.stopAutoplayCountdown()
            return
        allReadyMessage = getMessage("all-users-ready").format(self.userlist.readyUserCount())
        autoplayingMessage = getMessage("autoplaying-notification").format(int(self.autoplayTimeLeft))
        countdownMessage = "{}{}{}".format(allReadyMessage, self._player.osdMessageSeparator, autoplayingMessage)
        self.ui.showOSDMessage(countdownMessage, 1, OSDType=constants.OSD_ALERT, mood=constants.MESSAGE_GOODNEWS)
        if self.autoplayTimeLeft <= 0:
            self.setPaused(False)
            self.stopAutoplayCountdown()
        else:
            self.autoplayTimeLeft -= 1

    def resetAutoPlayState(self):
        self.autoPlay = False
        self.ui.updateAutoPlayState(False)
        self.stopAutoplayCountdown()

    @requireServerFeature("afk")
    def toggleAfk(self):
        # No optimistic local state - the server echoes the change back via Set:afk.
        self._protocol.setAfk(not self.userlist.currentUser.isAfk())

    @requireServerFeature("afk")
    def toggleAfkWithPause(self):
        # Player-keybind entry point: going AFK pauses the room first (unless it is
        # already paused), then marks you AFK. The server no longer treats a pause as
        # "returned" activity, so the later pause echo won't clear the AFK we just set.
        # Toggling back off just clears AFK - it leaves the pause state alone.
        if not self.userlist.currentUser.isAfk() and not self.getPlayerPaused():
            self._afkKeybindPausePending = True
            self.setPaused(True)
        self.toggleAfk()

    def toggleRoomLock(self):
        # Player-keybind (Ctrl+L) entry point. The room's lock state lives server-side, so the
        # toggle is resolved there (like /afk); non-admins just get the private "unauthorised"
        # reply. Degrades to an unknown-command warning on stock/old servers.
        self.sendChat(constants.TOGGLE_LOCK_COMMAND)

    @requireServerFeature("afk")
    def changeAfkState(self, newState):
        if bool(newState) != self.userlist.currentUser.isAfk():
            self.toggleAfk()

    @requireServerFeature("setOthersAfk")
    def setOthersAfk(self, username, newState):
        # Mirrors setOthersReadiness: the server checks control authority and echoes the
        # result back via Set:afk (with setBy), so there is no optimistic local state.
        self._protocol.setAfk(bool(newState), username)

    def setAfk(self, username, isAfk, setBy=None):
        oldAfkState = self.userlist.isAfk(username)
        self.userlist.setAfk(username, isAfk)
        self.ui.userListChange()
        if oldAfkState != isAfk:
            setByOther = setBy and setBy != username
            if username == self.userlist.currentUser.username:
                if setByOther:
                    self.ui.showMessage(getMessage("set-afk-by-other-notification" if isAfk else "set-not-afk-by-other-notification").format(setBy))
                else:
                    self.ui.showMessage(getMessage("set-as-afk-notification" if isAfk else "set-as-not-afk-notification"))
            elif self.userlist.isRoomSame(self.userlist.getUserRoom(username)):
                if setByOther:
                    self.ui.showMessage(getMessage("other-set-afk-notification" if isAfk else "other-set-not-afk-notification").format(username, setBy))
                else:
                    self.ui.showMessage(getMessage("other-afk-notification" if isAfk else "other-not-afk-notification").format(username))

    def setRoomLocked(self, roomName, locked, setBy=None):
        # Server-pushed lock state for a plain room (Set:roomLock). It makes canControl() false for
        # everyone but admins, which is what turns a pause keypress into a readiness toggle - see
        # _toggleReady. The room-wide "X locked this room" chat line is broadcast separately by the
        # server, so nothing is announced here.
        if not roomName:
            return
        self.userlist.setRoomLocked(roomName, locked)
        self.ui.showDebugMessage("Room '{}' {} by {}".format(roomName, "locked" if locked else "unlocked", setBy))
        self.ui.userListChange()

    @requireServerFeature("readiness")
    def toggleReady(self, manuallyInitiated=True):
        self._protocol.setReady(not self.userlist.currentUser.isReady(), manuallyInitiated)

    @requireServerFeature("readiness")
    def changeReadyState(self, newState, manuallyInitiated=True):
        oldState = self.userlist.currentUser.isReady()
        if newState != oldState:
            self.toggleReady(manuallyInitiated)

    def setReady(self, username, isReady, manuallyInitiated=True, setBy=None):
        oldReadyState = self.userlist.isReady(username)
        if oldReadyState is None:
            oldReadyState = False
        self.userlist.setReady(username, isReady)
        self.ui.userListChange()
        if oldReadyState != isReady:
            self._warnings.checkReadyStates()
        if setBy:
            if isReady:
                self.ui.showMessage(getMessage("other-set-as-ready-notification").format(username, setBy))
            else:
                self.ui.showMessage(getMessage("other-set-as-not-ready-notification").format(username, setBy))

    @requireServerFeature("managedRooms")
    def setUserFeatures(self, username, features):
        self.userlist.setFeatures(username, features)
        self.ui.userListChange()

    @requireServerFeature("managedRooms")
    def createControlledRoom(self, roomName):
        controlPassword = utils.RandomStringGenerator.generate_room_password()
        self.lastControlPasswordAttempt = controlPassword
        self._protocol.requestControlledRoom(roomName, controlPassword)

    def controlledRoomCreated(self, roomName, controlPassword):
        self.ui.showMessage(getMessage("created-controlled-room-notification").format(roomName, controlPassword, roomName, roomName + ":" + controlPassword))
        self.setRoom(roomName, resetAutoplay=True)
        self.sendRoom()
        self._protocol.requestControlledRoom(roomName, controlPassword)
        self.ui.updateRoomName(roomName)

    def stripControlPassword(self, controlPassword):
        if controlPassword:
            return re.sub(constants.CONTROL_PASSWORD_STRIP_REGEX, "", controlPassword).upper()
        else:
            return ""

    def identifyAsController(self, controlPassword):
        controlPassword = self.stripControlPassword(controlPassword)
        self.ui.showMessage(getMessage("identifying-as-controller-notification").format(controlPassword))
        self.lastControlPasswordAttempt = controlPassword
        self._protocol.requestControlledRoom(self.getRoom(), controlPassword)

    def controllerIdentificationError(self, username, room):
        if username == self.getUsername():
            self.ui.showErrorMessage(getMessage("failed-to-identify-as-controller-notification").format(username))

    def controllerIdentificationSuccess(self, username, roomname):
        self.userlist.setUserAsController(username)
        if self.userlist.isRoomSame(roomname):
            hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
            self.ui.showMessage(getMessage("authenticated-as-controller-notification").format(username), hideFromOSD)
            if username == self.userlist.currentUser.username:
                self.storeControlPassword(roomname, self.lastControlPasswordAttempt)
        self.ui.userListChange()

    def storeControlPassword(self, room, password):
        if password:
            self.controlpasswords[room] = password
            try:
                if self._config['autosaveJoinsToList']:
                    self.ui.addRoomToList(room+":"+password)
            except:
                pass

    def getControlledRoomPassword(self, room):
        if room in self.controlpasswords:
            return self.controlpasswords[room]

    def _markOverlayStartupSuccessful(self):
        # Fork auto-update: the client survived startup, so the active overlay is good.
        # Also surfaces a bootstrap quarantine from this boot (docs/auto-update.md).
        try:
            from syncplay import updater
            updater.markStartupSuccessful()
            quarantinedRelease = updater.getQuarantinedRelease()
            if quarantinedRelease:
                self.ui.showErrorMessage(
                    getMessage("update-overlay-disabled-after-crash-notification").format(quarantinedRelease))
        except Exception:
            pass

    def checkForOverlayUpdate(self, userInitiated):
        """Fork auto-update check (docs/auto-update.md). Blocking; returns
        (status, message, url, manifestOrNone) — unlike checkForUpdate, the 4th slot is the
        overlay manifest when an installable update exists, never a public-server list."""
        from syncplay import updater
        return updater.checkForUpdate(self._config, userInitiated)

    def checkForUpdate(self, userInitiated):
        """Upstream's syncplay.pl version check. The fork GUI no longer calls it (see
        ui/gui.py:checkForUpdates); kept intact so it stays mergeable with upstream."""
        try:
            import urllib.request, urllib.parse, urllib.error, syncplay, sys, json, platform
            try:
                architecture = platform.architecture()[0]
            except:
                architecture = "Unknown"
            try:
                machine = platform.machine()
            except:
                machine = "Unknown"
            params = urllib.parse.urlencode({'version': syncplay.version, 'milestone': syncplay.milestone, 'release_number': syncplay.release_number, 'language': syncplay.messages.messages["CURRENT"], 'platform': sys.platform, 'architecture': architecture, 'machine': machine, 'userInitiated': userInitiated})
            if isMacOS():
                import requests
                response = requests.get(constants.SYNCPLAY_UPDATE_URL.format(params))
                response = response.text
            else:
                f = urllib.request.urlopen(constants.SYNCPLAY_UPDATE_URL.format(params))
                response = f.read()
                response = response.decode('utf-8')
            response = response.replace("<p>", "").replace("</p>", "").replace("<br />", "").replace("&#8220;", "\"").replace("&#8221;", "\"")  # Fix Wordpress
            response = json.loads(response)
            publicServers = None
            if response["public-servers"]:
                publicServers = response["public-servers"].\
                    replace("&#8221;", "'").replace(":&#8217;", "'").replace("&#8217;", "'").replace("&#8242;", "'").replace("\n", "").replace("\r", "")
                publicServers = ast.literal_eval(publicServers)
            return response["version-status"], response["version-message"] if "version-message" in response\
                else None, response["version-url"] if "version-url" in response else None, publicServers
        except Exception as e:
            return "failed", str(e)+"\n-----\n"+getMessage("update-check-failed-notification").format(syncplay.version), constants.SYNCPLAY_DOWNLOAD_URL, None

    class _WarningManager(object):
        def __init__(self, player, userlist, ui, client):
            self._client = client
            self._player = player
            self._userlist = userlist
            self._ui = ui
            self._warnings = {
                "room-file-differences": {
                    "timer": task.LoopingCall(self.__displayMessageOnOSD, "room-file-differences",
                                              lambda: self._checkRoomForSameFiles(OSDOnly=True),),
                    "displayedFor": 0,
                },
                "alone-in-the-room": {
                    "timer": task.LoopingCall(self.__displayMessageOnOSD, "alone-in-the-room",
                                              lambda: self._checkIfYouReAloneInTheRoom(OSDOnly=True)),
                    "displayedFor": 0,
                },
                "not-all-ready": {
                    "timer": task.LoopingCall(self.__displayMessageOnOSD, "not-all-ready",
                                              lambda: self.checkReadyStates(),),
                    "displayedFor": 0,
                },
            }
            self.pausedTimer = task.LoopingCall(self.__displayPausedMessagesOnOSD)
            self.pausedTimer.start(constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL, True)

        def checkWarnings(self):
            if self._client.autoplayConditionsMet():
                return
            self._checkIfYouReAloneInTheRoom(OSDOnly=False)
            self._checkRoomForSameFiles(OSDOnly=False)
            self.checkReadyStates()

        def _checkRoomForSameFiles(self, OSDOnly):
            if not self._userlist.areAllFilesInRoomSame():
                self._displayReadySameWarning()
                if not OSDOnly and constants.SHOW_OSD_WARNINGS and not self._warnings["room-file-differences"]['timer'].running:
                    self._warnings["room-file-differences"]['timer'].start(constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL, True)
            elif self._warnings["room-file-differences"]['timer'].running:
                self._warnings["room-file-differences"]['timer'].stop()

        def _checkIfYouAreOnlyUserInRoomWhoSupportsReadiness(self):
            self._userlist._onlyUserInRoomWhoSupportsReadiness()

        def _checkIfYouReAloneInTheRoom(self, OSDOnly):
            if self._userlist.areYouAloneInRoom():
                self._ui.showOSDMessage(getMessage("alone-in-the-room"), constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL, OSDType=constants.OSD_ALERT, mood=constants.MESSAGE_BADNEWS)
                if not OSDOnly:
                    self._ui.showMessage(getMessage("alone-in-the-room"), True)
                    if constants.SHOW_OSD_WARNINGS and not self._warnings["alone-in-the-room"]['timer'].running:
                        self._warnings["alone-in-the-room"]['timer'].start(constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL, True)
            elif self._warnings["alone-in-the-room"]['timer'].running:
                self._warnings["alone-in-the-room"]['timer'].stop()

        def checkReadyStates(self):
            if not self._client:
                return
            if self._client.getPlayerPaused() or not self._userlist.currentUser.isReady() or not self._userlist.areAllRelevantUsersInRoomReady():
                self._warnings["not-all-ready"]["displayedFor"] = 0
            if self._userlist.areYouAloneInRoom():
                if self._warnings["not-all-ready"]['timer'].running:
                    self._warnings["not-all-ready"]['timer'].stop()
            elif not self._userlist.areAllRelevantUsersInRoomReady():
                self._displayReadySameWarning()
                if constants.SHOW_OSD_WARNINGS and not self._warnings["not-all-ready"]['timer'].running:
                    self._warnings["not-all-ready"]['timer'].start(constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL, True)
            elif self._warnings["not-all-ready"]['timer'].running:
                self._warnings["not-all-ready"]['timer'].stop()
                self._displayReadySameWarning()
            elif self._client.getPlayerPaused() or not self._userlist.currentUser.isReady():
                self._displayReadySameWarning()

        def _displayReadySameWarning(self):
            if not self._client._player or self._client.autoplayTimerIsRunning():
                return
            osdMessage = None
            messageMood = constants.MESSAGE_GOODNEWS
            fileDifferencesForRoom = self._userlist.getFileDifferencesForRoom()
            if not self._userlist.areAllFilesInRoomSame() and fileDifferencesForRoom is not None:
                messageMood = constants.MESSAGE_BADNEWS
                fileDifferencesMessage = getMessage("room-file-differences").format(fileDifferencesForRoom)
                if self._userlist.currentUser.canControl() and self._userlist.isReadinessSupported():
                    if self._userlist.areAllUsersInRoomReady():
                        allReadyMessage = getMessage("all-users-ready").format(self._userlist.readyUserCount())
                        osdMessage = "{}{}{}".format(fileDifferencesMessage, self._client._player.osdMessageSeparator, allReadyMessage)
                    else:
                        notAllReadyMessage = self._notReadyOSDMessage()
                        osdMessage = "{}{}{}".format(fileDifferencesMessage, self._client._player.osdMessageSeparator, notAllReadyMessage)
                else:
                    osdMessage = fileDifferencesMessage
            elif self._userlist.isReadinessSupported():
                if self._userlist.areAllUsersInRoomReady():
                    osdMessage = getMessage("all-users-ready").format(self._userlist.readyUserCount())
                else:
                    messageMood = constants.MESSAGE_BADNEWS
                    osdMessage = self._notReadyOSDMessage()
            if osdMessage:
                self._ui.showOSDMessage(osdMessage, constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL, OSDType=constants.OSD_ALERT, mood=messageMood)

        def _notReadyOSDMessage(self):
            # AFK users are forced not-ready, but call them out on their own line
            # rather than lumping them in with users who are simply not ready.
            parts = []
            notReady = self._userlist.usersInRoomNotReady(excludeAfk=True)
            if notReady:
                parts.append(getMessage("not-all-ready").format(notReady))
            afkUsers = self._userlist.usersInRoomAfk()
            if afkUsers:
                parts.append(getMessage("afk-osd-notification").format(afkUsers))
            return self._client._player.osdMessageSeparator.join(parts)

        def __displayMessageOnOSD(self, warningName, warningFunction):
            if constants.OSD_WARNING_MESSAGE_DURATION > self._warnings[warningName]["displayedFor"]:
                warningFunction()
                self._warnings[warningName]["displayedFor"] += constants.WARNING_OSD_MESSAGES_LOOP_INTERVAL
            else:
                self._warnings[warningName]["displayedFor"] = 0
                try:
                    self._warnings[warningName]["timer"].stop()
                except:
                    pass

        def __displayPausedMessagesOnOSD(self):
            if self._client.autoplayConditionsMet():
                return
            if self._client and self._client._player and self._client.getPlayerPaused():
                self._checkRoomForSameFiles(OSDOnly=True)
                self.checkReadyStates()
            elif not self._userlist.currentUser.isReady():  # CurrentUser should always be reminded they are set to not ready
                self.checkReadyStates()


class SyncplayUser(object):
    def __init__(self, username=None, room=None, file_=None):
        self.ready = None
        self.afk = False
        self.username = username
        self.room = room
        self.file = file_
        self._controller = False
        self._roomLocked = False  # Plain room locked by a server admin (see SyncplayUserlist.setRoomLocked)
        self._features = {}

    def setFile(self, filename, duration, size, path=None):
        file_ = {
            "name": filename,
            "duration": duration,
            "size": size,
            "path": path
        }
        self.file = file_

    def isFileSame(self, file_):
        if not self.file:
            return False
        sameName = utils.sameFilename(self.file['name'], file_['name'])
        sameSize = utils.sameFilesize(self.file['size'], file_['size'])
        sameDuration = utils.sameFileduration(self.file['duration'], file_['duration'])
        return sameName and sameSize and sameDuration

    def __lt__(self, other):
        if self.isController() == other.isController():
            return self.username.lower() < other.username.lower()
        else:
            return self.isController() > other.isController()

    def __repr__(self, *args, **kwargs):
        if self.file:
            return "{}: {} ({}, {})".format(self.username, self.file['name'], self.file['duration'], self.file['size'])
        else:
            return "{}".format(self.username)

    def setControllerStatus(self, isController):
        self._controller = isController

    def isController(self):
        return self._controller

    def setRoomLocked(self, locked):
        self._roomLocked = locked

    def isRoomLocked(self):
        return self._roomLocked

    def canControl(self):
        if self.isController():
            return True  # room operator, or a server admin (the fork flags admins as controllers)
        elif self._roomLocked:
            return False  # plain room locked by a server admin: only admins control it
        elif not utils.RoomPasswordProvider.isControlledRoom(self.room):
            return True
        else:
            return False

    def isReadyWithFile(self):
        if self.file is None:
            return None
        return self.ready

    def isReady(self):
        return self.ready

    def setReady(self, ready):
        self.ready = ready

    def isAfk(self):
        return self.afk

    def setAfk(self, afk):
        self.afk = afk

    def setFeatures(self, features):
        self._features = features


class SyncplayUserlist(object):
    # Rooms a server admin has locked, as last reported by the server. Class-level and replaced
    # rather than mutated, so it reads sanely on a userlist that has not been through __init__.
    _lockedRooms = frozenset()

    def __init__(self, ui, client):
        self.currentUser = SyncplayUser()
        self._users = {}
        self.ui = ui
        self._client = client
        self._roomUsersChanged = True

    def isReadinessSupported(self, requiresOtherUsers=True):
        if not utils.meetsMinVersion(self._client.serverVersion, constants.USER_READY_MIN_VERSION):
            return False
        elif self.onlyUserInRoomWhoSupportsReadiness() and requiresOtherUsers:
            return False
        else:
            return self._client.serverFeatures["readiness"]

    def isRoomSame(self, room):
        if room and self.currentUser.room and self.currentUser.room == room:
            return True
        else:
            return False

    def setRoomLocked(self, roomName, locked):
        # The lock is a property of the room, but canControl() is asked of a user, so it is stamped
        # onto every user known to be in that room (and re-stamped whenever someone joins or moves).
        lockedRooms = set(self._lockedRooms)
        if locked:
            lockedRooms.add(roomName)
        else:
            lockedRooms.discard(roomName)
        self._lockedRooms = lockedRooms
        for user in list(self._users.values()) + [self.currentUser]:
            if user.room == roomName:
                user.setRoomLocked(locked)

    def isRoomLocked(self, roomName=None):
        if roomName is None:
            roomName = self.currentUser.room
        return roomName in self._lockedRooms

    def clearRoomLocks(self):
        self._lockedRooms = frozenset()
        for user in list(self._users.values()) + [self.currentUser]:
            user.setRoomLocked(False)

    def __showUserChangeMessage(self, username, room, file_, oldRoom=None):
        if room:
            if self.isRoomSame(room) or self.isRoomSame(oldRoom):
                showOnOSD = constants.SHOW_OSD_WARNINGS
            else:
                showOnOSD = constants.SHOW_DIFFERENT_ROOM_OSD
            if constants.SHOW_NONCONTROLLER_OSD == False and self.canControl(username) == False:
                showOnOSD = False
            hideFromOSD = not showOnOSD
            if not file_:
                message = getMessage("room-join-notification").format(username, room)
                self.ui.showMessage(message, hideFromOSD)
            else:
                duration = utils.formatTime(file_['duration'])
                message = getMessage("playing-notification").format(username, file_['name'], duration)
                if self.currentUser.room != room or self.currentUser.username == username:
                    message += getMessage("playing-notification/room-addendum").format(room)
                self.ui.showMessage(message, hideFromOSD)
                if username == self.currentUser.username:
                    self._client.watched.maybeShowPlaylistWarningNotificationForFilename(self._client.playlist, file_['name'])
                if self.currentUser.file and not self.currentUser.isFileSame(file_) and self.currentUser.room == room:
                    fileDifferences = self.getFileDifferencesForUser(self.currentUser.file, file_)
                    if fileDifferences is not None:
                        message = getMessage("file-differences-notification").format(fileDifferences)
                        self.ui.showMessage(message, True)

    def getFileDifferencesForUser(self, currentUserFile, otherUserFile):
        if not currentUserFile or not otherUserFile:
            return None
        differences = []
        differentName = not utils.sameFilename(currentUserFile['name'], otherUserFile['name'])
        differentSize = not utils.sameFilesize(currentUserFile['size'], otherUserFile['size'])
        differentDuration = not utils.sameFileduration(currentUserFile['duration'], otherUserFile['duration'])
        if differentName:     differences.append(getMessage("file-difference-filename"))
        if differentSize:     differences.append(getMessage("file-difference-filesize"))
        if differentDuration: differences.append(getMessage("file-difference-duration"))
        return ", ".join(differences)

    def getFileDifferencesForRoom(self):
        if not self.currentUser.file:
            return None
        differences = []
        differentName = False
        differentSize = False
        differentDuration = False
        for otherUser in self._users.values():
            if otherUser.room == self.currentUser.room and otherUser.file:
                if not utils.sameFilename(self.currentUser.file['name'], otherUser.file['name']):
                    differentName = True
                if not utils.sameFilesize(self.currentUser.file['size'], otherUser.file['size']):
                    differentSize = True
                if not utils.sameFileduration(self.currentUser.file['duration'], otherUser.file['duration']):
                    differentDuration = True
        if differentName:     differences.append(getMessage("file-difference-filename"))
        if differentSize:     differences.append(getMessage("file-difference-filesize"))
        if differentDuration: differences.append(getMessage("file-difference-duration"))
        return ", ".join(differences)

    def addUser(self, username, room, file_, noMessage=False, isController=None, isReady=None, features={}, isAfk=False):
        if username == self.currentUser.username:
            if isController is not None:
                self.currentUser.setControllerStatus(isController)
            self.currentUser.setReady(isReady)
            self.currentUser.setAfk(isAfk)
            return
        user = SyncplayUser(username, room, file_)
        if isController is not None:
            user.setControllerStatus(isController)
        user.setRoomLocked(self.isRoomLocked(room))
        self._users[username] = user
        user.setReady(isReady)
        user.setAfk(isAfk)
        user.setFeatures(features)
        if not noMessage:
            self.__showUserChangeMessage(username, room, file_)
        self.userListChange(room)

    def removeUser(self, username):
        hideFromOSD = not constants.SHOW_DIFFERENT_ROOM_OSD
        if username in self._users:
            user = self._users[username]
            if user.room:
                if self.isRoomSame(user.room):
                    hideFromOSD = not constants.SHOW_SAME_ROOM_OSD
        if username in self._users:
            self._users.pop(username)
            message = getMessage("left-notification").format(username)
            self.ui.showMessage(message, hideFromOSD)
            self._client.lastLeftTime = time.time()
            self._client.lastLeftUser = username
        self.userListChange()

    def __displayModUserMessage(self, username, room, file_, user, oldRoom):
        if file_ and not user.isFileSame(file_):
            self.__showUserChangeMessage(username, room, file_, oldRoom)
        elif room and room != user.room:
            self.__showUserChangeMessage(username, room, None, oldRoom)

    def modUser(self, username, room, file_):
        if username in self._users:
            user = self._users[username]
            oldRoom = user.room if user.room else None
            if user.room != room:
                user.setControllerStatus(isController=False)
                user.setRoomLocked(self.isRoomLocked(room))
            self.__displayModUserMessage(username, room, file_, user, oldRoom)
            user.room = room
            if file_:
                user.file = file_
        elif username == self.currentUser.username:
            self.__showUserChangeMessage(username, room, file_)
        else:
            self.addUser(username, room, file_)
        self.userListChange(room)

    def setUserAsController(self, username):
        if self.currentUser.username == username:
            self.currentUser.setControllerStatus(True)
        elif username in self._users:
            user = self._users[username]
            user.setControllerStatus(True)

    def areAllRelevantUsersInRoomReady(self, requireSameFilenames=False):
        if not self.currentUser.isReady():
            return False
        if self.currentUser.canControl():
            return self.areAllUsersInRoomReady(requireSameFilenames)
        else:
            for user in self._users.values():
                if user.room == self.currentUser.room and user.canControl():
                    if user.isReadyWithFile() == False:
                        return False
                    elif (
                            requireSameFilenames and
                            (
                                    self.currentUser.file is None
                                    or user.file is None
                                    or not utils.sameFilename(self.currentUser.file['name'], user.file['name'])
                            )
                    ):
                        return False
        return True

    def areAllUsersInRoomReady(self, requireSameFilenames=False):
        if not self.currentUser.isReady():
            return False
        for user in self._users.values():
            if user.room == self.currentUser.room:
                if user.isReadyWithFile() == False:
                    return False
                elif (
                        requireSameFilenames and
                        (
                                self.currentUser.file is None
                                or user.file is None
                                or not utils.sameFilename(self.currentUser.file['name'], user.file['name'])
                        )
                ):
                    return False
        return True

    def areAllOtherUsersInRoomReady(self):
        for user in self._users.values():
            if user.room == self.currentUser.room and user.isReadyWithFile() == False:
                return False
        return True

    def readyUserCount(self):
        readyCount = 0
        if self.currentUser.isReady():
            readyCount += 1
        for user in self._users.values():
            if user.room == self.currentUser.room and user.isReadyWithFile():
                readyCount += 1
        return readyCount

    def usersInRoomCount(self):
        userCount = 1
        for user in self._users.values():
            if user.room == self.currentUser.room and user.isReadyWithFile():
                userCount += 1
        return userCount

    def usersInRoomNotReady(self, excludeAfk=False):
        notReady = []
        if not self.currentUser.isReady() and not (excludeAfk and self.currentUser.isAfk()):
            notReady.append(self.currentUser.username)
        for user in self._users.values():
            if user.room == self.currentUser.room and user.isReadyWithFile() == False and not (excludeAfk and user.isAfk()):
                notReady.append(user.username)
        return ", ".join(notReady)

    def usersInRoomAfk(self):
        afk = []
        if self.currentUser.isAfk():
            afk.append(self.currentUser.username)
        for user in self._users.values():
            if user.room == self.currentUser.room and user.isAfk():
                afk.append(user.username)
        return ", ".join(afk)

    def areAllFilesInRoomSame(self):
        if self.currentUser.file:
            for user in self._users.values():
                if user.room == self.currentUser.room and user.file and not self.currentUser.isFileSame(user.file):
                    if user.canControl():
                        return False
        return True

    def areYouAloneInRoom(self):
        if self._client.recentlyConnected():
            return False
        for user in self._users.values():
            if user.room == self.currentUser.room:
                return False
        return True

    def onlyUserInRoomWhoSupportsReadiness(self):
        for user in self._users.values():
            if user.room == self.currentUser.room and user.isReadyWithFile() is not None:
                return False
        return True

    def isUserInYourRoom(self, username):
        for user in self._users.values():
            if user.username == username and user.room == self.currentUser.room:
                return True
        return False

    def canControl(self, username):
        if self.currentUser.username == username and self.currentUser.canControl():
            return True

        for user in self._users.values():
            if user.username == username and user.canControl():
                return True
        return False

    def isReadyWithFile(self, username):
        if self.currentUser.username == username:
            return self.currentUser.isReadyWithFile()

        for user in self._users.values():
            if user.username == username:
                return user.isReadyWithFile()
        return None

    def isReady(self, username):
        if self.currentUser.username == username:
            return self.currentUser.isReady()

        for user in self._users.values():
            if user.username == username:
                return user.isReady()
        return None

    def setReady(self, username, isReady):
        if self.currentUser.username == username:
            self.currentUser.setReady(isReady)
        elif username in self._users:
            self._users[username].setReady(isReady)
        self._client.autoplayCheck()

    def isAfk(self, username):
        if self.currentUser.username == username:
            return self.currentUser.isAfk()
        for user in self._users.values():
            if user.username == username:
                return user.isAfk()
        return False

    def getUserRoom(self, username):
        if self.currentUser.username == username:
            return self.currentUser.room
        for user in self._users.values():
            if user.username == username:
                return user.room
        return None

    def setAfk(self, username, isAfk):
        if self.currentUser.username == username:
            self.currentUser.setAfk(isAfk)
        elif username in self._users:
            self._users[username].setAfk(isAfk)

    def userListChange(self, room=None):
        if room is not None and self.isRoomSame(room):
            self._roomUsersChanged = True
        self.ui.userListChange()

    def roomStateConfirmed(self):
        self._roomUsersChanged = False

    def hasRoomStateChanged(self):
        return self._roomUsersChanged

    def showUserList(self, altUI=None):
        rooms = {}
        for user in self._users.values():
            if user.room not in rooms:
                rooms[user.room] = []
            rooms[user.room].append(user)
        if self.currentUser.room not in rooms:
                rooms[self.currentUser.room] = []
        rooms[self.currentUser.room].append(self.currentUser)
        rooms = self.sortList(rooms)
        if altUI:
            altUI.showUserList(self.currentUser, rooms)
        else:
            self.ui.showUserList(self.currentUser, rooms)
        self._client.autoplayCheck()

    def clearList(self):
        self._users = {}

    def sortList(self, rooms):
        for room in rooms:
            rooms[room] = sorted(rooms[room])
        rooms = collections.OrderedDict(sorted(list(rooms.items()), key=lambda s: s[0].lower()))
        return rooms


class UiManager(object):
    def __init__(self, client, ui):
        self._client = client
        self.__ui = ui
        self.lastNotificatinOSDMessage = None
        self.lastNotificationOSDEndTime = None
        self.lastAlertOSDMessage = None
        self.lastAlertOSDEndTime = None
        self.lastError = ""
        self._yapTimerWasPaused = False
        self._lastBufferHoldText = ""  # so the "no hold" clear is sent once, not on every state tick
        self._pendingTrackProposals = []  # proposals that arrived before the player was ready
        self._pendingTrackProposalsArmed = False

    def getUIMode(self):
        return self.__ui.uiMode

    def addFileToPlaylist(self, newPlaylistItem):
        self.__ui.addFileToPlaylist(newPlaylistItem)

    def setPlaylist(self, newPlaylist, newIndexFilename=None):
        self.__ui.setPlaylist(newPlaylist, newIndexFilename)

    def setPlaylistIndexFilename(self, filename):
        self.__ui.setPlaylistIndexFilename(filename)

    def fileSwitchFoundFiles(self):
        self.__ui.fileSwitchFoundFiles()

    def setFeatures(self, featureList):
        self.__ui.setFeatures(featureList)

    def showDebugMessage(self, message):
        if constants.DEBUG_MODE and message.rstrip():
            sys.stderr.write("{}{}\n".format(time.strftime(constants.UI_TIME_FORMAT, time.localtime()), message.rstrip()))

    def updateYapTimer(self, paused, current, total, afkTotal=0, duration=None):
        # Server-driven live yap timer (only sent to players that advertised yapTimer support).
        # While paused we refresh every state tick so the overlay stays visible and counts up; on
        # resume we show the final total once and let the player's overlay auto-hide. The per-file
        # total is split into "active" (nobody AFK) vs "AFK" time; afkTotal is 0 from older servers.
        # A "drag" clause (total as a % of the file runtime) is appended when the server reports a
        # duration; older servers omit it (None) so nothing is shown.
        if not self._client._player:
            return
        active = max(0, total - afkTotal)
        pct = utils.dragRatioPercent(total, duration)
        dragSuffix = getMessage("yap-timer-drag-suffix").format(pct) if pct is not None else ""
        if paused:
            self._yapTimerWasPaused = True
            # The overlay renders as two stacked rows: the live "Yap timer: <current>" line, then a
            # detail line (total split + drag) beneath it. The separator is split back into rows lua-side.
            detail = getMessage("yap-timer-osd-paused-detail-message").format(
                utils.formatTime(total), utils.formatTime(active), utils.formatTime(afkTotal)) + dragSuffix
            text = getMessage("yap-timer-osd-paused-message").format(utils.formatTime(current)) \
                + constants.YAP_TIMER_OSD_ROW_SEPARATOR + detail
            self._client._player.updateYapTimerOSD(text)
        elif self._yapTimerWasPaused:
            self._yapTimerWasPaused = False
            text = getMessage("yap-timer-osd-total-message").format(
                utils.formatTime(total), utils.formatTime(active), utils.formatTime(afkTotal)) + dragSuffix
            self._client._player.updateYapTimerOSD(text)

    def updatePauseWarning(self, message):
        # Server sends this only while a pause is over the threshold; the player's overlay blinks it and
        # auto-hides shortly after the server stops sending it (on resume). No client-side clear needed.
        if self._client._player:
            self._client._player.updatePauseWarningOSD(message)

    def updateBufferHold(self, values):
        """Live overlay naming whoever the room is waiting for. None means no hold is in force.

        Values come off the wire, so nothing here trusts them: the username is truncated, the
        elapsed time is clamped to something a clock can show and a cache percentage outside 0-100
        is simply not displayed. The lua element auto-hides once refreshes stop, so an absent hold
        needs no explicit clear - but sending one costs nothing and makes the transition instant.
        """
        if not self._client._player:
            return
        if not isinstance(values, dict):
            # Sent once, not on every tick: with no hold in force this runs each second forever,
            # and the overlay is already gone.
            if self._lastBufferHoldText != "":
                self._lastBufferHoldText = ""
                self._client._player.updateBufferHoldOSD("")
            return
        username = str(values.get("user") or "")[:constants.MAX_USERNAME_LENGTH]
        try:
            elapsed = max(0.0, min(float(values.get("elapsed") or 0), constants.BUFFER_HOLD_MAX))
        except (TypeError, ValueError):
            elapsed = 0.0
        text = getMessage("buffer-hold-osd-message").format(username, utils.formatTime(elapsed))
        cache = values.get("cache")
        if isinstance(cache, (int, float)) and not isinstance(cache, bool) and 0 <= cache <= 100:
            text += getMessage("buffer-hold-osd-cache-suffix").format(int(cache))
        self._lastBufferHoldText = text
        self._client._player.updateBufferHoldOSD(text)

    def showGenericOSD(self, values):
        # Server-driven generic OSD message (Set:osdMessage). Values come off the wire, so defaults
        # and clamps are re-applied here regardless of what the server claims.
        text = values.get("text") if isinstance(values, dict) else None
        if not text or not isinstance(text, str):
            return
        text = text.replace("\r", "").replace("\n", "")[:constants.OSD_MESSAGE_MAX_LENGTH]
        isAss = bool(values.get("ass", False))
        colour = values.get("colour")
        if not isinstance(colour, str) or not re.match(r"^#[0-9A-Fa-f]{6}$", colour):
            colour = constants.OSD_MESSAGE_DEFAULT_COLOUR
        position = values.get("position")
        assAlignment = constants.OSD_MESSAGE_POSITIONS.get(
            position, constants.OSD_MESSAGE_POSITIONS[constants.OSD_MESSAGE_DEFAULT_POSITION])
        try:
            size = int(values.get("size"))
        except (TypeError, ValueError):
            size = constants.OSD_MESSAGE_DEFAULT_SIZE
        size = max(constants.OSD_MESSAGE_MIN_SIZE, min(constants.OSD_MESSAGE_MAX_SIZE, size))
        try:
            duration = float(values.get("duration"))
        except (TypeError, ValueError):
            duration = constants.OSD_MESSAGE_DEFAULT_DURATION
        duration = max(0.5, min(constants.OSD_MESSAGE_MAX_DURATION, duration))
        # Log the plain-text rendition (what fallback clients see as chat) to the GUI/console.
        logText = re.sub(constants.OSD_MESSAGE_STRIP_ASS_REGEX, "", text)
        logText = logText.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ").strip()
        if logText:
            self.showMessage(logText, noPlayer=True)
        if self._client._player:
            self._client._player.showGenericOSD(text, isAss, assAlignment, colour, size, duration)

    def setTrackProposal(self, values):
        # Admin-recommended default tracks (Set:trackProposal). Log it and hand the payload to the
        # player's lua, which applies it on layout match and shows the status-aware receipt OSD
        # ("Applied ..." vs pending recommendation - only the lua knows the apply outcome).
        if not isinstance(values, dict):
            return
        def describe(idKey, nameKey):
            if values.get(nameKey):
                return str(values[nameKey])
            value = values.get(idKey)
            if value == "no":
                return "off"
            return "#{}".format(value) if value is not None else "-"
        text = getMessage("track-proposal-osd-message").format(
            values.get("by", ""), describe("audioId", "audioName"), describe("subId", "subName"))
        self.showMessage(text, noPlayer=True)
        if self._client._player:
            self._client._player.setTrackProposal(values)
            return
        # No player yet: the connection beats the player start-up often enough that dropping the
        # payload here silently loses the room's recommendation on a cold start. Queue it and flush
        # once the player is up; the lua re-applies on every file-loaded, so late delivery is fine.
        self._pendingTrackProposals.append(values)
        while len(self._pendingTrackProposals) > constants.TRACK_CACHE_MAX_ENTRIES:
            self._pendingTrackProposals.pop(0)  # evict the oldest layout
        if not self._pendingTrackProposalsArmed:
            self._pendingTrackProposalsArmed = True
            self._client.addPlayerReadyCallback(lambda x: self._flushTrackProposals())

    def _flushTrackProposals(self):
        pending, self._pendingTrackProposals = self._pendingTrackProposals, []
        self._pendingTrackProposalsArmed = False
        if not self._client._player:
            return
        for proposal in pending:
            self._client._player.setTrackProposal(proposal)

    def showChatMessage(self, username, userMessage):
        messageString = "<{}> {}".format(username, userMessage)
        if self._client._player and self._client._player.chatOSDSupported and self._client._config["chatOutputEnabled"]:
            self._client._player.displayChatMessage(username, userMessage)
        else:
            self.showOSDMessage(messageString, duration=constants.OSD_DURATION)
        self.__ui.showMessage(messageString)

    def setSSLMode(self, sslMode, sslInformation=""):
        self.__ui.setSSLMode(sslMode, sslInformation)

    def showMessage(self, message, noPlayer=False, noTimestamp=False, OSDType=constants.OSD_NOTIFICATION, mood=constants.MESSAGE_NEUTRAL, isMotd=False):
        if not noPlayer:
            self.showOSDMessage(message, duration=constants.OSD_DURATION, OSDType=OSDType, mood=mood)
        self.__ui.showMessage(message, noTimestamp=noTimestamp, isMotd=isMotd)

    def updateAutoPlayState(self, newState):
        self.__ui.updateAutoPlayState(newState)

    def showUserList(self, currentUser, rooms):
        self.__ui.showUserList(currentUser, rooms)

    def showOSDMessage(self, message, duration=constants.OSD_DURATION, OSDType=constants.OSD_NOTIFICATION, mood=constants.MESSAGE_NEUTRAL):
        if(isNoOSDMessage(message)):
            return

        autoplayConditionsMet = self._client.autoplayConditionsMet()
        if OSDType == constants.OSD_ALERT and not constants.SHOW_OSD_WARNINGS and not self._client.autoplayTimerIsRunning():
            return
        if not self._client._player:
            return
        if constants.SHOW_OSD and self._client and self._client._player:
            if not self._client._player.alertOSDSupported:
                if OSDType == constants.OSD_ALERT:
                    self.lastAlertOSDMessage = message
                    if autoplayConditionsMet:
                        self.lastAlertOSDEndTime = time.time() + 1.0
                    else:
                        self.lastAlertOSDEndTime = time.time() + constants.NO_ALERT_OSD_WARNING_DURATION
                    if self.lastNotificationOSDEndTime and time.time() < self.lastNotificationOSDEndTime:
                        message = "{}{}{}".format(message, self._client._player.osdMessageSeparator, self.lastNotificatinOSDMessage)
                else:
                    self.lastNotificatinOSDMessage = message
                    self.lastNotificationOSDEndTime = time.time() + constants.OSD_DURATION
                    if self.lastAlertOSDEndTime and time.time() < self.lastAlertOSDEndTime:
                        message = "{}{}{}".format(self.lastAlertOSDMessage, self._client._player.osdMessageSeparator, message)
            self._client._player.displayMessage(message, int(duration * 1000), OSDType, mood)

    def setControllerStatus(self, username, isController):
        self.__ui.setControllerStatus(username, isController)

    def showErrorMessage(self, message, criticalerror=False):
        if message != self.lastError:  # Avoid double call bug
            self.lastError = message
            self.__ui.showErrorMessage(message, criticalerror)

    def promptFor(self, prompt):
        return self.__ui.promptFor(prompt)

    def userListChange(self):
        self.__ui.userListChange()

    def markEndOfUserlist(self):
        self.__ui.markEndOfUserlist()

    def updateRoomName(self, room=""):
        self.__ui.updateRoomName(room)

    def addRoomToList(self, room):
        self.__ui.addRoomToList(room)

    def executeCommand(self, command):
        self.__ui.executeCommand(command)

    def drop(self):
        self.__ui.drop()


class SyncplayPlaylist():
    def __init__(self, client):
        self.queuedIndex = None
        self._client = client
        self._ui = self._client.ui
        self._previousPlaylist = None
        self._previousPlaylistRoom = None
        self._playlist = []
        self._playlistIndex = None
        self.addedChangeListCallback = False
        self.switchToNewPlaylistItem = False
        self._lastPlaylistIndexChange = time.time()
        self.lastNearEOFName = None
        self.lastNearEOFPath = None
        self.lastNearEOFPlayedTime = 0.0
        self.lastNearEOFLastTime = 0.0
        self.lastNearEOFWasPlaying = False
        self.lastOwnPlaylistIndexEchoName = None
        self.lastOwnPlaylistIndexEchoTime = 0.0

    def clearNearEOFMarker(self):
        self.lastNearEOFName = None
        self.lastNearEOFPath = None
        self.lastNearEOFPlayedTime = 0.0
        self.lastNearEOFLastTime = 0.0
        self.lastNearEOFWasPlaying = False

    def needsSharedPlaylistsEnabled(f):  # @NoSelf
        @wraps(f)
        def wrapper(self, *args, **kwds):
            if not self._client.sharedPlaylistIsEnabled():
                self._ui.showDebugMessage("Tried to use shared playlists when it was disabled!")
                return
            return f(self, *args, **kwds)
        return wrapper

    def openedFile(self):
        self._lastPlaylistIndexChange = time.time()

    def removeDirsFromPath(self, filePath):
        if os.path.isfile(filePath):
            return os.path.basename(filePath)
        elif utils.isURL(filePath):
            return filePath
        self._ui.showDebugMessage("Could not find path: {}".format(filePath))

    def getPlaylistIndexFromPath(self, filePath):
        filePath = self.removeDirsFromPath(filePath)
        try:
            return self._playlist.index(filePath)
        except ValueError:
            return

    def changeToPlaylistIndexFromFilename(self, filename):
        try:
            index = self._playlist.index(filename)
            if index != self._playlistIndex:
                self.changeToPlaylistIndex(index, resetPosition=True)
            else:
                if filename == self.queuedIndexFilename:
                    return
                self._client.rewindFile()
        except ValueError:
            pass

    def loadDelayedPath(self, changeToIndex):
        # Implementing the behaviour set out at https://github.com/Syncplay/syncplay/issues/315

        if not self._client:
            return

        if self._client.playerIsNotReady():
            self._client.addPlayerReadyCallback(lambda x: self.loadDelayedPath(changeToIndex))
            return

        if self._client._protocol and self._client._protocol.hadFirstPlaylistIndex and self._client.delayedLoadPath:
            delayedLoadPath = str(self._client.delayedLoadPath)
            self._client.delayedLoadPath = None
            if self._client.sharedPlaylistIsEnabled():
                pathWithoutDirs = self.removeDirsFromPath(delayedLoadPath)
                if len(self._playlist) == 0:
                    self._client.openFile(delayedLoadPath, resetPosition=True, fromUser=True)
                    self._client.ui.addFileToPlaylist(delayedLoadPath)
                else:
                    try:
                        currentPlaylistFilename = self._playlist[changeToIndex]
                    except TypeError:
                        currentPlaylistFilename = None
                    if currentPlaylistFilename != pathWithoutDirs:
                        if pathWithoutDirs not in self._playlist:
                            if utils.isURL(delayedLoadPath) or utils.isURL(currentPlaylistFilename):
                                self._client.ui.addFileToPlaylist(delayedLoadPath)
                            else:
                                foundFilePath = self._client.fileSwitch.findFilepath(currentPlaylistFilename, highPriority=True)
                                if foundFilePath is None:
                                    self._client.openFile(delayedLoadPath, resetPosition=False)
                                else:
                                    self._client.ui.showMessage("{}: {}...".format(getMessage("addfilestoplaylist-menu-label"), pathWithoutDirs))
                                    reactor.callLater(constants.DELAYED_LOAD_WAIT_TIME, self._client.ui.addFileToPlaylist, delayedLoadPath, ) # TODO: Avoid arbitary pause
                        else:
                            self._client.ui.showErrorMessage(getMessage("cannot-add-duplicate-error").format(pathWithoutDirs))

            else:
                self._client.openFile(delayedLoadPath)

    def changeToPlaylistIndex(self, index, username=None, resetPosition=False):
        if self.loadDelayedPath(index):
            return
        if self._playlist is None or len(self._playlist) == 0:
            return
        if index is None:
            return
        if username is None and not self._client.sharedPlaylistIsEnabled():
            return
        self._lastPlaylistIndexChange = time.time()
        if self._client.playerIsNotReady():
            if not self.addedChangeListCallback:
                self.addedChangeListCallback = True
                self._client.addPlayerReadyCallback(lambda x: self.changeToPlaylistIndex(index, username, resetPosition))
            return
        try:
            filename = self._playlist[index]
            self._ui.setPlaylistIndexFilename(filename)
            if username == self._client.getUsername():
                self.lastOwnPlaylistIndexEchoName = filename
                self.lastOwnPlaylistIndexEchoTime = time.time()
            if not self._client.sharedPlaylistIsEnabled():
                self._playlistIndex = index
            if username is not None and self._client.userlist.currentUser.file and utils.sameFilename(filename, self._client.userlist.currentUser.file['name']):
                if not self.queuedIndexFilename or utils.sameFilename(self.queuedIndexFilename, filename):
                    self._playlistIndex = index
                    return
                self._ui.showDebugMessage(
                    "Not treating '{}' as already loaded because '{}' is still queued to load.".format(
                        filename, self.queuedIndexFilename))
        except IndexError:
            pass

        self._playlistIndex = index
        if username is None:
            if self._client.isConnectedAndInARoom() and self._client.sharedPlaylistIsEnabled():
                self._client.setPlaylistIndex(index)
                filename = self._playlist[index]
                self._ui.setPlaylistIndexFilename(filename)
                if resetPosition:
                    self._ui.showDebugMessage("Pausing due to index change")
                    state = {}
                    state["playstate"] = {}
                    state["playstate"]["position"] = 0
                    state["playstate"]["paused"] = True
                    self._client.lastAdvanceTime = time.time()
                    self._client._protocol and self._client._protocol.sendMessage({"State": state})
                    self._playerPaused = True
                    self._client.autoplayCheck()
                    self.doubleCheckForWatchedPreviousFile()
        elif index is not None:
            filename = self._playlist[index]
            self._ui.setPlaylistIndexFilename(filename)
            self._ui.showMessage(getMessage("playlist-selection-changed-notification").format(username))
            self.switchToNewPlaylistIndex(index, resetPosition=resetPosition)

    def canSwitchToNextPlaylistIndex(self):
        if self._thereIsNextPlaylistIndex() and self._client.sharedPlaylistIsEnabled():
            try:
                index = self._nextPlaylistIndex()
                if index is None:
                    return False
                filename = self._playlist[index]
                if utils.isURL(filename):
                    return True if self._client.isURITrusted(filename) else False
                else:
                    path = self._client.fileSwitch.findFilepath(filename, highPriority=True)
                return True if path else False
            except:
                return False
        return False

    @needsSharedPlaylistsEnabled
    def switchToNewPlaylistIndex(self, index, resetPosition = False):
        try:
            self.queuedIndexFilename = self._playlist[index]
        except:
            self.queuedIndexFilename = None
            self._ui.showDebugMessage("Failed to find index {} in playlist".format(index))
        if resetPosition and index is not None:
            filename = self._playlist[index]
            if (not utils.isURL(filename)) or self._client.isURITrusted(filename):
                self._client.prepareToChangeToNewPlaylistItemAndRewind()

        self._lastPlaylistIndexChange = time.time()
        if self._client.playerIsNotReady():
            self._client.addPlayerReadyCallback(lambda x: self.switchToNewPlaylistIndex(index, resetPosition))
            return

        try:
            if index is None:
                self._ui.showDebugMessage("Cannot switch to None index in playlist")
                return
            filename = self._playlist[index]
            # TODO: Handle isse with index being None
            if utils.isURL(filename):
                if self._client.isURITrusted(filename):
                    self._client.openFile(filename, resetPosition=resetPosition)
                else:
                    self._ui.showErrorMessage(getMessage("cannot-add-unsafe-path-error").format(filename))
                return
            else:
                path = self._client.fileSwitch.findFilepath(filename, highPriority=True)
            if path:
                self._client.openFile(path, resetPosition=resetPosition)
            else:
                self._ui.showErrorMessage(getMessage("cannot-find-file-for-playlist-switch-error").format(filename))
                return
        except IndexError:
            self._ui.showDebugMessage("Could not change playlist index due to IndexError")

    def _getValidIndexFromNewPlaylist(self, newPlaylist=None):
        if self.switchToNewPlaylistItem:
            self.switchToNewPlaylistItem = False
            return len(self._playlist)

        if self._playlistIndex is None or not newPlaylist or len(newPlaylist) <= 1:
            return 0

        i = self._playlistIndex
        while i <= len(self._playlist):
            try:
                filename = self._playlist[i]
                validIndex = newPlaylist.index(filename)
                return validIndex
            except:
                i += 1

        i = self._playlistIndex
        while i > 0:
            try:
                filename = self._playlist[i]
                validIndex = newPlaylist.index(filename)
                return validIndex+1 if validIndex < len(newPlaylist)-1 else validIndex
            except:
                i -= 1
        return 0

    def _getFilenameFromIndexInGivenPlaylist(self, _playlist, _index):
        if not _index or not _playlist:
            return None
        filename = _playlist[_index] if len(_playlist) > _index else None
        return filename

    def loadPlaylistFromFile(self, path, shuffle=False):
        if not os.path.isfile(path):
            self._ui.showDebugMessage("Not loading {} as file could not be found".format(path))
            return

        with open(path) as f:
            newPlaylist = f.read().splitlines()
            if path.lower().endswith(".m3u8"):
                newPlaylist = [
                    line for line in newPlaylist if line.strip() and not line.startswith("#")
                ]
            if shuffle:
                random.shuffle(newPlaylist)
            if newPlaylist:
                self.changePlaylist(newPlaylist, username=None, resetIndex=True)

    def savePlaylistToFile(self, path):
        with open(path, 'w') as playlistFile:
            playlistToSave = utils.getListAsMultilineString(self._playlist)
            playlistFile.write(playlistToSave)
            self._ui.showMessage("Playlist saved as {}".format(path)) # TODO: Move to messages_en

    def playlistNeedsRestoring(self, files, username):
        if self._client.playlistMayNeedRestoring:
            self._client.playlistMayNeedRestoring = False
            return self._client.sharedPlaylistIsEnabled() and self._playlist != None and files == [] and username == None and not self._playlistBufferIsFromOldRoom(self._client.userlist.currentUser.room)

    def changePlaylist(self, files, username=None, resetIndex=False):
        if self.playlistNeedsRestoring(files, username):
            self._ui.showDebugMessage("Restoring playlist on reconnect...")
            files = self._playlist.copy()
            self._client._protocol and self._client._protocol.setPlaylist(files)
            self._client._protocol and self._client._protocol.setPlaylistIndex(self._playlistIndex)
            return
        self.queuedIndexFilename = None
        self._client.playlistMayNeedRestoring = False
        if self._playlist == files:
            if self._playlistIndex != 0 and resetIndex:
                self.changeToPlaylistIndex(0)
            return

        if resetIndex:
            newIndex = 0
            filename = files[0] if files and len(files) > 0 else None
        else:
            newIndex = self._getValidIndexFromNewPlaylist(files)
            filename = self._getFilenameFromIndexInGivenPlaylist(files, newIndex)

        self._updateUndoPlaylistBuffer(newPlaylist=files, newRoom=self._client.userlist.currentUser.room)
        self._playlist = files

        if username is None:
            if self._client.isConnectedAndInARoom() and self._client.sharedPlaylistIsEnabled():
                self._client._protocol and self._client._protocol.setPlaylist(files)
                self.changeToPlaylistIndex(newIndex)
                self._ui.setPlaylist(self._playlist, filename)
                self._ui.showMessage(getMessage("playlist-contents-changed-notification").format(self._client.getUsername()))
        else:
            self._ui.setPlaylist(self._playlist)
            self._ui.showMessage(getMessage("playlist-contents-changed-notification").format(username))
        self.doubleCheckForWatchedPreviousFile()

    def addToPlaylist(self, file):
        self.changePlaylist([*self._playlist, file])

    def deleteAtIndex(self, index):
        new_playlist = self._playlist.copy()
        if index >= 0 and index < len(new_playlist):
            del new_playlist[index]
            self.changePlaylist(new_playlist)
        else:
            raise TypeError("Invalid index")


    @needsSharedPlaylistsEnabled
    def undoPlaylistChange(self):
        if self.canUndoPlaylist(self._playlist):
            newPlaylist = self._getPreviousPlaylist()
            self.changePlaylist(newPlaylist, username=None)

    @needsSharedPlaylistsEnabled
    def shuffleRemainingPlaylist(self):
        if self._playlist and len(self._playlist) > 0:
            shuffledPlaylist = deepcopy(self._playlist)
            shufflePoint = self._playlistIndex + 1
            partToKeep = shuffledPlaylist[:shufflePoint]
            partToShuffle = shuffledPlaylist[shufflePoint:]
            random.shuffle(partToShuffle)
            shuffledPlaylist = partToKeep + partToShuffle
            self.changePlaylist(shuffledPlaylist, username=None, resetIndex=False)

    @needsSharedPlaylistsEnabled
    def shuffleEntirePlaylist(self):
        if self._playlist and len(self._playlist) > 0:
            shuffledPlaylist = deepcopy(self._playlist)
            random.shuffle(shuffledPlaylist)
            self.changePlaylist(shuffledPlaylist, username=None, resetIndex=True)
            self.switchToNewPlaylistIndex(0, resetPosition=True)

    def canUndoPlaylist(self, currentPlaylist):
        return self._previousPlaylist is not None and currentPlaylist != self._previousPlaylist

    def loadCurrentPlaylistIndex(self):
        if self._notPlayingCurrentIndex():
            self.switchToNewPlaylistIndex(self._playlistIndex)

    @needsSharedPlaylistsEnabled
    def advancePlaylistCheck(self):
        position = self._client.getStoredPlayerPosition()
        currentLength = self._client.userlist.currentUser.file["duration"] if self._client.userlist.currentUser.file else 0
        if currentLength <= 0:
            return
        if (
            currentLength > constants.PLAYLIST_LOAD_NEXT_FILE_MINIMUM_LENGTH and
            abs(position - currentLength) < constants.PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD and
            self.notJustChangedPlaylist()
        ):
            watchedIndex = self._playlistIndex
            watchedFilename = None
            expectedNextFilename = None
            if watchedIndex is not None and self._playlist and watchedIndex >= 0 and watchedIndex < len(self._playlist):
                watchedFilename = self._playlist[watchedIndex]
            if self._thereIsNextPlaylistIndex():
                nextIndex = self._nextPlaylistIndex()
                if nextIndex is not None and self._playlist and nextIndex >= 0 and nextIndex < len(self._playlist):
                    expectedNextFilename = self._playlist[nextIndex]
            self.clearNearEOFMarker()
            self._client.watched.markCurrentFileWatched()
            self.loadNextFileInPlaylist()
            if watchedFilename and expectedNextFilename and not utils.sameFilename(watchedFilename, expectedNextFilename):
                self.scheduleAutoRemoveWatchedPlaylistItem(watchedFilename, watchedIndex, expectedNextFilename)

    @needsSharedPlaylistsEnabled
    def recordPlayedNearEOF(self, paused, position):
        if not self._client.userlist.currentUser.file:
            self.lastNearEOFWasPlaying = False
            return
        if position == None:
            self.lastNearEOFWasPlaying = False
            return
        currentLength = self._client.userlist.currentUser.file["duration"] if self._client.userlist.currentUser.file else 0
        if currentLength <= 0:
            self.lastNearEOFWasPlaying = False
            return
        isPlaying = paused is False
        remainingTime = currentLength - position
        nearEOFWindow = min(constants.PLAYLIST_NEAR_EOF_WINDOW, currentLength / 2)

        if (
            isPlaying and
            remainingTime < nearEOFWindow and
            currentLength > constants.PLAYLIST_LOAD_NEXT_FILE_MINIMUM_LENGTH and
            self.notJustChangedPlaylist()
        ):
            now_monotime = time.monotonic()
            if self.lastNearEOFName != self._client.userlist.currentUser.file['name']:
                self.lastNearEOFName = self._client.userlist.currentUser.file['name']
                self.lastNearEOFPath = self._client.userlist.currentUser.file['path']
                self.lastNearEOFPlayedTime = 0.0
                self.lastNearEOFWasPlaying = False
            if self.lastNearEOFWasPlaying:
                elapsed = now_monotime - self.lastNearEOFLastTime
                if elapsed <= constants.PLAYLIST_NEAR_EOF_LATCH_TTL:
                    self.lastNearEOFPlayedTime += max(0.0, elapsed)
            self.lastNearEOFLastTime = now_monotime
            self.lastNearEOFWasPlaying = True
        else:
            self.lastNearEOFWasPlaying = False

    @needsSharedPlaylistsEnabled
    def doubleCheckForWatchedPreviousFile(self):
        if not self.lastNearEOFName or not self.lastNearEOFPath:
            return False

        if self._playingSpecificFilename(self.lastNearEOFName):
            return False

        now_monotime = time.monotonic()
        if self.lastNearEOFPlayedTime < constants.WATCHED_NEAR_EOF_MINIMUM_TIME:
            self.clearNearEOFMarker()
            return False

        age = now_monotime - self.lastNearEOFLastTime
        if age > constants.PLAYLIST_NEAR_EOF_LATCH_TTL:
            self.clearNearEOFMarker()
            return False
        filePath = self.lastNearEOFPath
        self.clearNearEOFMarker()
        self._client.watched.markFileWatched(filePath)
        return True

    def _getIndexOfFilenameInPlaylist(self, playlist, filename, expectedIndex=None):
        if not playlist:
            return None

        if (
            expectedIndex is not None and
            expectedIndex >= 0 and
            expectedIndex < len(playlist) and
            utils.sameFilename(playlist[expectedIndex], filename)
        ):
            return expectedIndex

        for index, playlistFilename in enumerate(playlist):
            if utils.sameFilename(playlistFilename, filename):
                return index

        return None

    def scheduleAutoRemoveWatchedPlaylistItem(self, filename, expectedIndex=None, expectedNextFilename=None):
        if not constants.AUTO_REMOVE_WATCHED_FROM_PLAYLIST:
            return
        if not filename or not expectedNextFilename or utils.sameFilename(filename, expectedNextFilename):
            return
        deadline = time.time() + constants.PLAYLIST_AUTO_REMOVE_WATCHED_TIMEOUT
        scheduledRoom = self._client.userlist.currentUser.room
        scheduledAfter = time.time()
        reactor.callLater(
            constants.PLAYLIST_AUTO_REMOVE_WATCHED_RECHECK_INTERVAL,
            self._autoRemoveWatchedPlaylistItemWhenSafe,
            filename,
            deadline,
            expectedIndex,
            expectedNextFilename,
            scheduledRoom,
            scheduledAfter)

    def _autoRemoveWatchedPlaylistItemWhenSafe(self, filename, deadline, expectedIndex=None, expectedNextFilename=None, scheduledRoom=None, scheduledAfter=0.0):
        if not constants.AUTO_REMOVE_WATCHED_FROM_PLAYLIST:
            return
        if not filename or not expectedNextFilename:
            return
        if self._client.userlist.currentUser.room != scheduledRoom:
            self._ui.showDebugMessage(
                "Not auto-removing watched playlist item '{}' because the room changed while waiting.".format(filename))
            return
        if self._getIndexOfFilenameInPlaylist(self._playlist, filename, expectedIndex) is None:
            return
        if self._getIndexOfFilenameInPlaylist(self._playlist, expectedNextFilename) is None:
            return

        if not self._autoRemoveWatchedPlaylistItemIsSafe(filename, expectedNextFilename, scheduledRoom, scheduledAfter):
            if time.time() < deadline:
                reactor.callLater(
                    constants.PLAYLIST_AUTO_REMOVE_WATCHED_RECHECK_INTERVAL,
                    self._autoRemoveWatchedPlaylistItemWhenSafe,
                    filename,
                    deadline,
                    expectedIndex,
                    expectedNextFilename,
                    scheduledRoom,
                    scheduledAfter)
            else:
                self._ui.showDebugMessage(
                    "Not auto-removing watched playlist item '{}' because the room did not settle on '{}'.".format(
                        filename, expectedNextFilename))
            return

        self.autoRemoveWatchedPlaylistItem(filename, expectedIndex)

    def _autoRemoveWatchedPlaylistItemIsSafe(self, watchedFilename, expectedNextFilename, scheduledRoom=None, scheduledAfter=0.0):
        return (
            self._roomHasMovedToExpectedNextFilename(watchedFilename, expectedNextFilename, scheduledRoom) and
            self._playlistIndexEchoReceivedForExpectedNextFilename(expectedNextFilename, scheduledAfter)
        )

    def _playlistIndexEchoReceivedForExpectedNextFilename(self, expectedNextFilename, scheduledAfter):
        if self.lastOwnPlaylistIndexEchoTime < scheduledAfter:
            return False
        if not self.lastOwnPlaylistIndexEchoName:
            return False
        return utils.sameFilename(self.lastOwnPlaylistIndexEchoName, expectedNextFilename)

    def _roomHasMovedToExpectedNextFilename(self, watchedFilename, expectedNextFilename, scheduledRoom=None):
        if not watchedFilename or not expectedNextFilename:
            return False

        currentUser = self._client.userlist.currentUser
        if scheduledRoom is not None and currentUser.room != scheduledRoom:
            return False
        currentRoom = currentUser.room

        expectedNextIndex = self._getIndexOfFilenameInPlaylist(self._playlist, expectedNextFilename)
        if expectedNextIndex is None or self._playlistIndex != expectedNextIndex:
            return False

        if not currentUser.file:
            return False
        if utils.sameFilename(currentUser.file.get("name"), watchedFilename):
            return False
        if not utils.sameFilename(currentUser.file.get("name"), expectedNextFilename):
            return False

        for user in self._client.userlist._users.values():
            if user.room != currentRoom:
                continue
            if not user.file:
                return False
            if utils.sameFilename(user.file.get("name"), watchedFilename):
                return False
            if not utils.sameFilename(user.file.get("name"), expectedNextFilename):
                return False

        return True

    def autoRemoveWatchedPlaylistItem(self, filename, expectedIndex=None):
        if not constants.AUTO_REMOVE_WATCHED_FROM_PLAYLIST:
            return False
        if not filename:
            return False
        if not self._client.sharedPlaylistIsEnabled():
            return False
        if not self._client.isConnectedAndInARoom():
            return False
        if not self._client.userlist.currentUser.canControl():
            return False
        if not self._playlist:
            return False

        playlist = self._playlist.copy()
        removeIndex = self._getIndexOfFilenameInPlaylist(playlist, filename, expectedIndex)

        if removeIndex is None:
            self._ui.showDebugMessage(
                "Not auto-removing watched playlist item '{}' because it is no longer in the playlist.".format(filename))
            return False

        del playlist[removeIndex]
        self.changePlaylist(playlist, username=None, resetIndex=False)
        return True

    def notJustChangedPlaylist(self):
        secondsSinceLastChange = time.time() - self._lastPlaylistIndexChange
        return secondsSinceLastChange > constants.PLAYLIST_LOAD_NEXT_FILE_TIME_FROM_END_THRESHOLD

    @needsSharedPlaylistsEnabled
    def loadNextFileInPlaylist(self):
        if self._notPlayingCurrentIndex():
            return

        if len(self._playlist) == 1 and self._client.loopSingleFiles():
            self._lastPlaylistIndexChange = time.time()
            self._client.rewindFile()
            self._client.setPaused(False)
            reactor.callLater(0.5, self._client.setPaused, False,)

        elif self._thereIsNextPlaylistIndex():
            self._client.prepareToAdvancePlaylist()
            self.switchToNewPlaylistIndex(self._nextPlaylistIndex(), resetPosition=True)

    def _updateUndoPlaylistBuffer(self, newPlaylist, newRoom):
        if self._playlistBufferIsFromOldRoom(newRoom):
            self._movePlaylistBufferToNewRoom(newRoom)
        elif self._playlistBufferNeedsUpdating(newPlaylist):
            self._previousPlaylist = self._playlist

    def _getPreviousPlaylist(self):
        return self._previousPlaylist

    def _notPlayingCurrentIndex(self):
        if self._playlistIndex is None or self._playlist is None or len(self._playlist) <= self._playlistIndex:
            self._ui.showDebugMessage("Not playing current index - Index none or length issue")
            return True
        currentPlaylistFilename = self._playlist[self._playlistIndex]
        if self._client.userlist.currentUser.file and currentPlaylistFilename == self._client.userlist.currentUser.file['name']:
            return False
        else:
            self._ui.showDebugMessage("Not playing current index - Filename mismatch or no file")
            return True

    def _playingSpecificFilename(self, filenameToCompare):
        if self._client.userlist.currentUser.file:
            return self._client.userlist.currentUser.file['name'] == filenameToCompare
        else:
            return False

    def _thereIsNextPlaylistIndex(self):
        if self._playlistIndex is None:
            return False
        elif len(self._playlist) == 1 and not self._client.loopSingleFiles():
            return False
        elif self._playlistIsAtEnd():
            return self._client.isPlaylistLoopingEnabled()
        else:
            return True

    def _nextPlaylistIndex(self):
        if self._playlistIsAtEnd():
            return 0
        else:
            return self._playlistIndex+1

    def _playlistIsAtEnd(self):
        return len(self._playlist) <= self._playlistIndex+1

    def _playlistBufferIsFromOldRoom(self, newRoom):
        return self._previousPlaylistRoom != newRoom

    def _movePlaylistBufferToNewRoom(self, currentRoom):
        self._previousPlaylist = None
        self._previousPlaylistRoom = currentRoom

    def _playlistBufferNeedsUpdating(self, newPlaylist):
        return self._previousPlaylist != self._playlist and self._playlist != newPlaylist




class FileSwitchManager(object):
    def __init__(self, client):
        self._client = client
        self.mediaFilesCache = {}
        self.filenameWatchlist = []
        self.currentDirectory = None
        self.mediaDirectories = client.getConfig().get('mediaSearchDirectories')
        self.lock = threading.Lock()
        self.folderSearchEnabled = True
        self.directorySearchError = None
        self.newInfo = False
        self.currentlyUpdating = False
        self.updateInfoPending = False
        self.newWatchlist = []
        self.fileSwitchTimer = task.LoopingCall(self.updateInfo)
        self.fileSwitchTimer.start(constants.FOLDER_SEARCH_DOUBLE_CHECK_INTERVAL, True)
        self.mediaDirectoriesNotFound = []

    def setClient(self, newClient):
        self._client = newClient

    def setCurrentDirectory(self, curDir):
        self.currentDirectory = curDir

    def changeMediaDirectories(self, mediaDirs):
        from syncplay.ui.ConfigurationGetter import ConfigurationGetter
        ConfigurationGetter().setConfigOption("mediaSearchDirectories", mediaDirs)
        self._client._config["mediaSearchDirectories"] = mediaDirs
        self._client.ui.showMessage(getMessage("media-directory-list-updated-notification"))
        self.mediaDirectoriesNotFound = []
        self.folderSearchEnabled = True
        self.setMediaDirectories(mediaDirs)
        if mediaDirs == "":
            self._client.ui.showErrorMessage(getMessage("no-media-directories-error"))
            self.mediaFilesCache = {}
            self.newInfo = True
            self.checkForFileSwitchUpdate()

    def setMediaDirectories(self, mediaDirs):
        self.mediaDirectories = mediaDirs
        self.updateInfo()

    def checkForFileSwitchUpdate(self):
        if self.newInfo:
            self.newInfo = False
            self.infoUpdated()
        if self.directorySearchError:
            self._client.ui.showErrorMessage(self.directorySearchError)
            self.directorySearchError = None
        self._client.playlist.doubleCheckForWatchedPreviousFile()

    def updateInfo(self, queueIfUpdating=False):
        if not self.mediaDirectories:
            return
        if self.currentlyUpdating:
            if queueIfUpdating:
                self.updateInfoPending = True
            return
        self.currentlyUpdating = True
        threads.deferToThread(self._updateInfoThread).addBoth(self._updateInfoFinished)

    def _updateInfoFinished(self, result):
        self.currentlyUpdating = False
        if self.updateInfoPending:
            self.updateInfoPending = False
            if self.folderSearchEnabled and self.mediaDirectories:
                self.directorySearchError = None
                self.updateInfo()
                return result
        self.checkForFileSwitchUpdate()
        return result

    def setFilenameWatchlist(self, unfoundFilenames):
        self.filenameWatchlist = unfoundFilenames

    def _updateInfoThread(self):
        with self.lock:
            try:
                dirsToSearch = self.mediaDirectories

                if not self.folderSearchEnabled:
                    return

                if dirsToSearch:
                    # Spin up hard drives to prevent premature timeout
                    randomFilename = "RandomFile"+str(random.randrange(10000, 99999))+".txt"
                    for directory in dirsToSearch:
                        if not os.path.isdir(directory):
                            self.directorySearchError = getMessage("cannot-find-directory-error").format(directory)

                        startTime = time.time()
                        if os.path.isfile(os.path.join(directory, randomFilename)):
                            randomFilename = "RandomFile"+str(random.randrange(10000, 99999))+".txt"
                            print("Found random file (?)")
                        if time.time() - startTime > constants.FOLDER_SEARCH_FIRST_FILE_TIMEOUT:
                            self.folderSearchEnabled = False
                            self.directorySearchError = getMessage("folder-search-first-file-timeout-error").format(directory)
                            return

                    # Actual directory search
                    newMediaFilesCache = {}
                    startTime = time.time()
                    fileCount = 0
                    lastWarningTime = None
                    for directory in dirsToSearch:
                        for root, dirs, files in os.walk(directory):
                            fileCount += 1
                            newMediaFilesCache[root] = files
                            timeTakenSoFar = time.time() - startTime
                            if timeTakenSoFar > constants.FOLDER_SEARCH_TIMEOUT:
                                reactor.callLater(0.1, self._client.ui.showErrorMessage, getMessage("folder-search-timeout-error").format(directory, fileCount),False)
                                self.folderSearchEnabled = False
                                return
                            if timeTakenSoFar > constants.FOLDER_SEARCH_WARNING_THRESHOLD:
                                if not lastWarningTime or timeTakenSoFar - lastWarningTime >= 1:
                                    reactor.callLater(0.1, self._client.ui.showErrorMessage, getMessage("folder-search-timeout-warning").format(int(timeTakenSoFar), fileCount, directory),False)
                                    lastWarningTime = timeTakenSoFar

                    if self.mediaFilesCache != newMediaFilesCache:
                        self.mediaFilesCache = newMediaFilesCache
                        self.newInfo = True
            except Exception as e:
                self._client.ui.showDebugMessage(str(e))

    def infoUpdated(self):
        self._client.fileSwitchFoundFiles()

    def findFilepath(self, filename, highPriority=False):
        if filename is None:
            return

        if self._client.userlist.currentUser.file and utils.sameFilename(filename, self._client.userlist.currentUser.file['name']):
            return utils.getCorrectedPathForFile(self._client.userlist.currentUser.file['path'])

        if self.mediaFilesCache is not None:
            for directory in self.mediaFilesCache:
                files = self.mediaFilesCache[directory]
                if len(files) > 0 and filename in files:
                    filepath = os.path.join(directory, filename)
                    filepath = utils.getCorrectedPathForFile(filepath)
                    if os.path.isfile(filepath):
                        return filepath

        if self.folderSearchEnabled and self.mediaDirectories is not None:
            directoryList = self.mediaDirectories
            for directory in directoryList:
                filepath = os.path.join(directory, filename)
                filepath = utils.getCorrectedPathForFile(filepath)
                if os.path.isfile(filepath):
                    return filepath

    def areWatchedFilenamesInCache(self):
        if self.filenameWatchlist is not None:
            for filename in self.filenameWatchlist:
                if self.isFilenameInCache(filename):
                    return True

    def isFilenameInCache(self, filename):
        if filename is not None and self.mediaFilesCache is not None:
            for directory in self.mediaFilesCache:
                files = self.mediaFilesCache[directory]
                if filename in files:
                    return True

    def getDirectoryOfFilenameInCache(self, filename):
        if filename is not None and self.mediaFilesCache is not None:
            for directory in self.mediaFilesCache:
                files = self.mediaFilesCache[directory]
                if filename in files:
                    filepath = os.path.join(directory, filename)
                    if os.path.isfile(filepath):
                        return directory
                    watched_directory = os.path.join(directory, constants.WATCHED_SUBFOLDER)
                    watched_filepath = os.path.join(directory, constants.WATCHED_SUBFOLDER, filename)
                    if os.path.isfile(watched_filepath):
                        return watched_directory
        return None

    def isDirectoryInList(self, directoryToFind, folderList):
        if directoryToFind and folderList:
            normedDirectoryToFind = os.path.normcase(os.path.normpath(directoryToFind))
            for listedFolder in folderList:
                normedListedFolder = os.path.normcase(os.path.normpath(listedFolder))
                if normedDirectoryToFind.startswith(normedListedFolder):
                    return True
            return False

    def notifyUserIfFileNotInMediaDirectory(self, filenameToFind, path):
        directoryToFind = os.path.dirname(path)
        if directoryToFind in self.mediaDirectoriesNotFound:
            return
        if self.mediaDirectories is not None and self.mediaFilesCache is not None:
            if directoryToFind in self.mediaFilesCache:
                return
            for directory in self.mediaFilesCache:
                files = self.mediaFilesCache[directory]
                if filenameToFind in files:
                    return
                if directoryToFind in self.mediaFilesCache:
                    return
        if self.isDirectoryInList(directoryToFind, self.mediaDirectories):
            return
        directoryToFind = str(directoryToFind)
        self._client.ui.showErrorMessage(getMessage("added-file-not-in-media-directory-error").format(directoryToFind))
        self.mediaDirectoriesNotFound.append(directoryToFind)
