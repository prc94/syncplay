import argparse
import codecs
import hashlib
import os
import re
import time
from string import Template

from twisted.enterprise import adbapi
from twisted.internet import task, reactor
from twisted.internet.protocol import Factory

try:
    from OpenSSL import crypto
    from OpenSSL.SSL import TLSv1_2_METHOD
    from twisted.internet import ssl
except:
    pass

import syncplay
from syncplay import constants
from syncplay.messages import getMessage
from syncplay.protocols import SyncServerProtocol
from syncplay.utils import RoomPasswordProvider, NotControlledRoom, RandomStringGenerator, meetsMinVersion, playlistIsValid, truncateText, getListAsMultilineString, convertMultilineStringToList, formatTime

class SyncFactory(Factory):
    def __init__(self, port='', password='', motdFilePath=None, roomsDbFile=None, permanentRoomsFile=None, isolateRooms=False, salt=None,
                 disableReady=False, disableChat=False, maxChatMessageLength=constants.MAX_CHAT_MESSAGE_LENGTH,
                 maxUsernameLength=constants.MAX_USERNAME_LENGTH, statsDbFile=None, tlsCertPath=None, yapTimer=False,
                 pauseWarningAfter=0, pauseWarningInterval=0, pauseWarningMessage=None, adminPassword=None):
        self.isolateRooms = isolateRooms
        self.yapTimer = yapTimer
        self.adminPassword = adminPassword if adminPassword else None  # Plaintext compare; use TLS
        syncplay.messages.setLanguage(syncplay.messages.getInitialLanguage())
        self.pauseWarningAfter = pauseWarningAfter if pauseWarningAfter else 0  # Secs; 0/None = feature off
        self.pauseWarningInterval = pauseWarningInterval if pauseWarningInterval else self.pauseWarningAfter  # Chat re-remind cadence
        self.pauseWarningMessage = pauseWarningMessage if pauseWarningMessage else getMessage("pause-warning-default-message")
        print(getMessage("welcome-server-notification").format(syncplay.version))
        self.port = port
        if password:
            password = password.encode('utf-8')
            password = hashlib.md5(password).hexdigest()
        self.password = password
        if salt is None:
            salt = RandomStringGenerator.generate_server_salt()
            print(getMessage("no-salt-notification").format(salt))
        self._salt = salt
        self._motdFilePath = motdFilePath
        self.roomsDbFile = roomsDbFile
        self.disableReady = disableReady
        self.disableChat = disableChat
        self.maxChatMessageLength = maxChatMessageLength if maxChatMessageLength is not None else constants.MAX_CHAT_MESSAGE_LENGTH
        self.maxUsernameLength = maxUsernameLength if maxUsernameLength is not None else constants.MAX_USERNAME_LENGTH
        self.permanentRoomsFile = permanentRoomsFile if permanentRoomsFile is not None and os.path.isfile(permanentRoomsFile) else None
        self.permanentRooms = self.loadListFromMultilineTextFile(self.permanentRoomsFile) if self.permanentRoomsFile is not None else []
        if not isolateRooms:
            self._roomManager = RoomManager(self.roomsDbFile, self.permanentRooms)
        else:
            self._roomManager = PublicRoomManager()
        if statsDbFile is not None:
            self._statsDbHandle = StatsDBManager(statsDbFile)
            self._statsRecorder = StatsRecorder(self._statsDbHandle, self._roomManager)
            statsDelay = 5*(int(self.port)%10 + 1)
            self._statsRecorder.startRecorder(statsDelay)
        else:
            self._statsDbHandle = None
        if tlsCertPath is not None:
            self.certPath = tlsCertPath
            self._TLSattempts = 0
            self._allowTLSconnections(self.certPath)
        else:
            self.certPath = None
            self.options = None
            self.serverAcceptsTLS = False
        # Per-room track-proposal cache: roomName -> {signature: proposal}. Runtime-only (not
        # persisted to the rooms DB) but deliberately NOT cleared on room-empty, so a layout we
        # have seen auto-reapplies to any later matching file without the admin re-publishing.
        self._trackCache = {}

    def loadListFromMultilineTextFile(self, path):
        if not os.path.isfile(path):
            return []
        with open(path) as f:
            multiline = f.read().splitlines()
        return multiline

    def loadRoom(self):
        rooms = self._roomsDbHandle.loadRooms()

    def buildProtocol(self, addr):
        return SyncServerProtocol(self)

    def sendState(self, watcher, doSeek=False, forcedUpdate=False):
        room = watcher.getRoom()
        if room:
            paused, position = room.isPaused(), room.getPosition()
            setBy = room.getSetBy()
            watcher.sendState(position, paused, doSeek, setBy, forcedUpdate)

    def getFeatures(self):
        features = dict()
        features["isolateRooms"] = self.isolateRooms
        features["readiness"] = not self.disableReady
        features["managedRooms"] = True
        features["persistentRooms"] = self.roomsDbFile is not None
        features["chat"] = not self.disableChat
        features["maxChatMessageLength"] = self.maxChatMessageLength
        features["maxUsernameLength"] = self.maxUsernameLength
        features["maxRoomNameLength"] = constants.MAX_ROOM_NAME_LENGTH
        features["maxFilenameLength"] = constants.MAX_FILENAME_LENGTH
        features["setOthersReadiness"] = True
        features["serverAdmin"] = self.adminPassword is not None
        features["afk"] = True

        return features

    def getMotd(self, userIp, username, room, clientVersion):
        oldClient = False
        if constants.WARN_OLD_CLIENTS:
            if not meetsMinVersion(clientVersion, constants.RECENT_CLIENT_THRESHOLD):
                oldClient = True
        if self._motdFilePath and os.path.isfile(self._motdFilePath):
            tmpl = codecs.open(self._motdFilePath, "r", "utf-8-sig").read()
            args = dict(version=syncplay.version, userIp=userIp, username=username, room=room)
            try:
                motd = Template(tmpl).substitute(args)
                if oldClient:
                    motdwarning = getMessage("new-syncplay-available-motd-message").format(clientVersion)
                    motd = "{}\n{}".format(motdwarning, motd)
                return motd if len(motd) < constants.SERVER_MAX_TEMPLATE_LENGTH else getMessage("server-messed-up-motd-too-long").format(constants.SERVER_MAX_TEMPLATE_LENGTH, len(motd))
            except ValueError:
                return getMessage("server-messed-up-motd-unescaped-placeholders")
        elif oldClient:
            return getMessage("new-syncplay-available-motd-message").format(clientVersion)
        else:
            return ""

    def addWatcher(self, watcherProtocol, username, roomName):
        roomName = truncateText(roomName, constants.MAX_ROOM_NAME_LENGTH)
        username = self._roomManager.findFreeUsername(username, self.maxUsernameLength)
        watcher = Watcher(self, watcherProtocol, username)
        self.setWatcherRoom(watcher, roomName, asJoin=True)

    def setWatcherRoom(self, watcher, roomName, asJoin=False):
        roomName = truncateText(roomName, constants.MAX_ROOM_NAME_LENGTH)
        self.setAfk(watcher, False)  # switching rooms is activity; broadcast reaches the old room
        self._roomManager.moveWatcher(watcher, roomName)
        if asJoin:
            self.sendJoinMessage(watcher)
        else:
            self.sendRoomSwitchMessage(watcher)

        room = watcher.getRoom()
        roomSetByName = room.getSetBy().getName() if room.getSetBy() else None
        watcher.setPlaylist(roomSetByName, room.getPlaylist())
        watcher.setPlaylistIndex(roomSetByName, room.getPlaylistIndex())
        if RoomPasswordProvider.isControlledRoom(roomName):
            for controller in room.getControllers():
                watcher.sendControlledRoomAuthStatus(True, controller, roomName)
        if watcher.isAdmin():
            self._broadcastAdminStatus(watcher)  # keep the operator icon in the new room
        cachedProposals = self._cachedTrackProposals(roomName)
        if watcher.supportsFeature("trackProposals") and cachedProposals:
            for proposal in cachedProposals:  # hand the capable client the whole per-room layout cache
                watcher.sendTrackProposal(proposal)
        elif room.getTrackProposal() is not None:
            self._sendTrackProposalToWatcher(watcher, room.getTrackProposal())  # late joiners get the recommendation
        if room.getTrustedDomains() is not None:
            self._sendTrustedDomainsToWatcher(watcher, room.getTrustedDomains())  # late joiners get the domains

    def sendRoomSwitchMessage(self, watcher):
        l = lambda w: w.sendSetting(watcher.getName(), watcher.getRoom(), None, None)
        self._roomManager.broadcast(watcher, l)
        self._roomManager.broadcastRoom(watcher, lambda w: w.sendSetReady(watcher.getName(), watcher.isReady(), False))
        self._broadcastAfkToRoom(watcher)
        if self.roomsDbFile:
            l = lambda w: w.sendList(toGUIOnly=True)
            self._roomManager.broadcast(watcher, l)

    def removeWatcher(self, watcher):
        if watcher and watcher.getRoom():
            room = watcher.getRoom()
            self.sendLeftMessage(watcher)
            self._roomManager.removeWatcher(watcher)
            if room.isEmpty():
                self._stopYapTicker(room)
                self._stopPauseWarningTimer(room)
                room.yapReset()
                room.setTrackProposal(None)
                room.setTrustedDomains(None)
            else:
                self._yapNoteAfkPresence(room)  # an AFK watcher may have just left
            if self.roomsDbFile:
                l = lambda w: w.sendList(toGUIOnly=True)
                self._roomManager.broadcast(watcher, l)

    def sendLeftMessage(self, watcher):
        l = lambda w: w.sendSetting(watcher.getName(), watcher.getRoom(), None, {"left": True})
        self._roomManager.broadcast(watcher, l)

    def sendJoinMessage(self, watcher):
        l = lambda w: w.sendSetting(watcher.getName(), watcher.getRoom(), None, {"joined": True, "version": watcher.getVersion(), "features": watcher.getFeatures()}) if w != watcher else None
        self._roomManager.broadcast(watcher, l)
        self._roomManager.broadcastRoom(watcher, lambda w: w.sendSetReady(watcher.getName(), watcher.isReady(), False))
        self._broadcastAfkToRoom(watcher)
        if self.roomsDbFile:
            l = lambda w: w.sendList(toGUIOnly=True)
            self._roomManager.broadcast(watcher, l)

    def sendFileUpdate(self, watcher):
        if watcher.getFile():
            l = lambda w: w.sendSetting(watcher.getName(), watcher.getRoom(), watcher.getFile(), None)
            self._roomManager.broadcast(watcher, l)
            self._yapNoteFileChange(watcher.getRoom())
            self._remindTrackProposalOnFileChange(watcher)

    def forcePositionUpdate(self, watcher, doSeek, watcherPauseState):
        room = watcher.getRoom()
        if room.canControl(watcher):
            paused, position = room.isPaused(), watcher.getPosition()
            setBy = watcher
            if doSeek:
                self._yapNoteRewind(room, position)
            l = lambda w: w.sendState(position, paused, doSeek, setBy, True)
            room.setPosition(watcher.getPosition(), setBy)
            self._roomManager.broadcastRoom(watcher, l)
        else:
            watcher.sendState(room.getPosition(), watcherPauseState, False, watcher, True)  # Fixes BC break with 1.2.x
            watcher.sendState(room.getPosition(), room.isPaused(), True, room.getSetBy(), True)

    def getAllWatchersForUser(self, forUser):
        return self._roomManager.getAllWatchersForUser(forUser)

    def getEmptyPersistentRooms(self):
        return self._roomManager.getEmptyPersistentRooms()

    def authRoomController(self, watcher, password, roomBaseName=None):
        room = watcher.getRoom()
        roomName = roomBaseName if roomBaseName else room.getName()
        try:
            success = RoomPasswordProvider.check(roomName, password, self._salt)
            if success:
                watcher.getRoom().addController(watcher)
            self._roomManager.broadcast(watcher, lambda w: w.sendControlledRoomAuthStatus(success, watcher.getName(), room._name))
        except NotControlledRoom:
            newName = RoomPasswordProvider.getControlledRoomName(roomName, password, self._salt)
            watcher.sendNewControlledRoom(newName, password)
        except ValueError:
            self._roomManager.broadcastRoom(watcher, lambda w: w.sendControlledRoomAuthStatus(False, watcher.getName(), room._name))

    def sendChat(self, watcher, message):
        # Server-command interception happens before chat truncation (/osd carries verbose ASS
        # markup with its own cap; /admin carries a password that must never reach the room).
        # First-token exact match; an unknown /command is warned back to the sender privately and
        # never broadcast (see the fall-through below), so client chat boxes can forward commands.
        if message.startswith("/"):
            command = message.split(" ", 1)[0].lower()
            if command == constants.OSD_MESSAGE_COMMAND:
                self._handleOSDChatCommand(watcher, message)
                return
            if command == constants.ADMIN_COMMAND:
                parts = message.split(" ", 1)
                self.authAdmin(watcher, parts[1].strip() if len(parts) > 1 else None)
                return
            if command == constants.LOCK_COMMAND:
                self._handleLockChatCommand(watcher, locked=True)
                return
            if command == constants.UNLOCK_COMMAND:
                self._handleLockChatCommand(watcher, locked=False)
                return
            if command == constants.TOGGLE_LOCK_COMMAND:
                # Ctrl+L in mpv (and typed /togglelock) - flip the room's lock. State lives
                # server-side, so the toggle is resolved here (like /afk does for readiness).
                room = watcher.getRoom()
                self._handleLockChatCommand(watcher, locked=(room is None or not room.isLocked()))
                return
            if command == constants.AFK_COMMAND:
                # Reaches the server only from stock clients (modded clients intercept /afk
                # locally and toggle via Set:afk) - toggle for them so anyone can use it.
                self.setAfk(watcher, not watcher.isAfk())
                return
            if command == constants.INFO_COMMAND:
                parts = message.split(" ", 1)
                argument = parts[1].strip().lower() if len(parts) > 1 else ""
                self._handleInfoChatCommand(watcher, full=(argument == constants.INFO_FULL_ARGUMENT))
                return
            if command == constants.PUBLISH_DOMAINS_COMMAND:
                # Only reaches the server from legacy clients (updated clients publish via Set).
                watcher.sendChatMessage({"message": getMessage("domains-command-notice-chat-message"),
                                         "username": watcher.getName()})
                return
            if command == constants.TRACK_PROPOSAL_COMMAND:
                # Reaches the server only from legacy/stock clients (modded clients intercept
                # locally and publish via Set:trackProposal) - explain what is needed.
                watcher.sendChatMessage({"message": getMessage("tracks-command-notice-chat-message"),
                                         "username": watcher.getName()})
                return
            # Unknown slash-command (e.g. one an updated client forwarded from its chat box):
            # warn the sender privately and do NOT broadcast it to the room. Only the command
            # token is echoed back, never the arguments (a mistyped /admin could carry a password).
            watcher.sendChatMessage({"message": getMessage("unknown-command-chat-message").format(command),
                                     "username": watcher.getName()})
            return
        self.setAfk(watcher, False)  # chatting is activity
        message = truncateText(message, self.maxChatMessageLength)
        messageDict = {"message": message, "username": watcher.getName()}
        self._roomManager.broadcastRoom(watcher, lambda w: w.sendChatMessage(messageDict))

    def authAdmin(self, watcher, password):
        # Shared by the /admin chat command and the Set:adminAuth message (modded-client auto-auth).
        # Replies are private chat lines so both paths work on any client without new handling.
        name = watcher.getName()
        if not self.adminPassword:
            watcher.sendChatMessage({"message": getMessage("admin-not-enabled-chat-message"), "username": name})
            return
        if not password or password != self.adminPassword:
            watcher.sendChatMessage({"message": getMessage("admin-login-fail-chat-message"), "username": name})
            return
        watcher.setAdmin(True)
        watcher.sendChatMessage({"message": getMessage("admin-login-success-chat-message"), "username": name})
        self._broadcastAdminStatus(watcher)

    def _broadcastAdminStatus(self, watcher):
        # Reuses the controller-status mechanism so the admin gets the operator icon (and their
        # own client unlocks its operator UI) on completely unmodified clients.
        room = watcher.getRoom()
        if room is not None:
            roomName = room.getName()
            self._roomManager.broadcast(watcher, lambda w: w.sendControlledRoomAuthStatus(True, watcher.getName(), roomName))

    def _handleLockChatCommand(self, watcher, locked):
        name = watcher.getName()
        if not watcher.isAdmin():
            watcher.sendChatMessage({"message": getMessage("admin-unauthorised-chat-message"), "username": name})
            return
        room = watcher.getRoom()
        if room is None:
            return
        if RoomPasswordProvider.isControlledRoom(room.getName()):
            watcher.sendChatMessage({"message": getMessage("room-already-managed-chat-message"), "username": name})
            return
        room.setLocked(locked)
        key = "room-locked-chat-message" if locked else "room-unlocked-chat-message"
        messageDict = {"message": getMessage(key).format(name), "username": name}
        self._roomManager.broadcastRoom(watcher, lambda w: w.sendChatMessage(messageDict))

    @staticmethod
    def _onOff(value):
        return getMessage("info-enabled-chat-message" if value else "info-disabled-chat-message")

    def _handleInfoChatCommand(self, watcher, full=False):
        # Report current server-side state privately to the sender only (never broadcast).
        # Room-level runtime state is always shown; server configuration is only appended on an
        # explicit "/info full" and restricted to admins and managed-room operators. Secrets are
        # only ever reported as present/absent.
        name = watcher.getName()
        room = watcher.getRoom()
        if room is None:
            watcher.sendChatMessage({"message": getMessage("info-unavailable-chat-message"), "username": name})
            return

        def send(message):
            watcher.sendChatMessage({"message": message, "username": name})

        # --- Room-level runtime state (everyone) ---
        send(getMessage("info-room-header-chat-message").format(room.getName()))

        if RoomPasswordProvider.isControlledRoom(room.getName()):
            lockState = getMessage("info-lock-managed-chat-message")
        elif room.isLocked():
            lockState = getMessage("info-lock-locked-chat-message")
        else:
            lockState = getMessage("info-lock-unlocked-chat-message")
        send(getMessage("info-lock-chat-message").format(lockState))

        proposal = room.getTrackProposal()
        send(getMessage("info-tracks-chat-message").format(
            self._trackProposalChatText(proposal) if proposal else getMessage("info-none-chat-message")))

        domains = room.getTrustedDomains()
        if domains and domains.get("domains"):
            domainsText = getMessage("info-domains-summary-chat-message").format(
                len(domains["domains"]), domains.get("by", ""), ", ".join(domains["domains"]))
        else:
            domainsText = getMessage("info-none-chat-message")
        send(truncateText(getMessage("info-domains-chat-message").format(domainsText), self.maxChatMessageLength))

        controllers = sorted({w.getName() for w in room.getWatchers() if w.isAdmin()}
                             | set(getattr(room, "_controllers", {}).keys()))
        send(getMessage("info-controllers-chat-message").format(
            ", ".join(controllers) if controllers else getMessage("info-none-chat-message")))

        # --- Server configuration (admins and managed-room operators, on "/info full") ---
        if not watcher.isController():
            return
        if not full:
            send(getMessage("info-full-hint-chat-message"))
            return
        features = self.getFeatures()
        send(getMessage("info-server-header-chat-message"))
        send(getMessage("info-server-general-chat-message").format(
            self._onOff(features["isolateRooms"]), self._onOff(features["readiness"]),
            self._onOff(features["chat"]), self._onOff(features["persistentRooms"])))
        send(getMessage("info-server-limits-chat-message").format(
            features["maxChatMessageLength"], features["maxUsernameLength"],
            features["maxRoomNameLength"], features["maxFilenameLength"]))
        send(getMessage("info-server-admin-chat-message").format(
            self._onOff(self.adminPassword is not None), self._onOff(bool(self.password)),
            self._onOff(self.serverAcceptsTLS), len(self.permanentRooms)))
        send(getMessage("info-server-yap-chat-message").format(
            self._onOff(self.yapTimer), self.pauseWarningAfter, self.pauseWarningInterval))

    def sendOSDMessage(self, room, text, senderName, colour=None, position=None, size=None,
                       duration=None, assFormatting=False):
        # Generic OSD message channel: capable clients get a styled (optionally full-ASS) overlay
        # via Set:osdMessage; everyone else gets the tag-stripped text as a chat line.
        if room is None or not text:
            return
        text = truncateText(text.replace("\r", "").replace("\n", ""), constants.OSD_MESSAGE_MAX_LENGTH)
        if colour is None or not re.match(r"^#[0-9A-Fa-f]{6}$", colour):
            colour = constants.OSD_MESSAGE_DEFAULT_COLOUR
        if position not in constants.OSD_MESSAGE_POSITIONS:
            position = constants.OSD_MESSAGE_DEFAULT_POSITION
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = constants.OSD_MESSAGE_DEFAULT_SIZE
        size = max(constants.OSD_MESSAGE_MIN_SIZE, min(constants.OSD_MESSAGE_MAX_SIZE, size))
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = constants.OSD_MESSAGE_DEFAULT_DURATION
        duration = max(0.5, min(constants.OSD_MESSAGE_MAX_DURATION, duration))
        payload = {
            "text": text,
            "ass": bool(assFormatting),
            "colour": colour,
            "position": position,
            "size": size,
            "duration": duration,
        }
        fallbackDict = {"message": self.stripOSDTags(text), "username": senderName}
        for receiver in room.getWatchers():
            if receiver.supportsFeature("osdMessages"):
                receiver.sendOSDMessage(payload)
            else:
                receiver.sendChatMessage(fallbackDict)

    @staticmethod
    def stripOSDTags(text):
        # Plain-text rendition of an (optionally ASS-styled) message for chat/log fallback.
        text = re.sub(constants.OSD_MESSAGE_STRIP_ASS_REGEX, "", text)
        return text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ").strip()

    @staticmethod
    def _parseOSDCommand(message):
        # "/osd [ass=1] [dur=N] [colour=#RRGGBB] [pos=POS] [size=N] text..." -> (options, text).
        # Leading key=value tokens are options; the first non-option token starts the message text.
        remainder = message[len(constants.OSD_MESSAGE_COMMAND):].strip()
        options = {}
        while remainder:
            token, _, rest = remainder.partition(" ")
            key, sep, value = token.partition("=")
            key = key.lower()
            if not sep or key not in ("ass", "dur", "duration", "colour", "color", "pos", "size"):
                break
            if key == "ass":
                options["assFormatting"] = value.lower() in ("1", "true", "yes", "on")
            elif key in ("dur", "duration"):
                options["duration"] = value
            elif key in ("colour", "color"):
                options["colour"] = value
            elif key == "pos":
                options["position"] = value.lower()
            elif key == "size":
                options["size"] = value
            remainder = rest.strip()
        return options, remainder

    def setTrustedDomains(self, watcher, payload):
        # An admin or room controller publishes their own client's trusted-domains list to the room.
        # Capable clients get Set:trustedDomains (merged session-only, subject to their opt-out);
        # others get an informational chat line. isController() covers admins everywhere plus managed-
        # room operators - the client has no separate admin bit, so its share checkbox uses the same
        # test (see gui.openSetTrustedDomainsDialog).
        if not watcher.isController():
            watcher.sendChatMessage({"message": getMessage("domains-unauthorised-chat-message"),
                                     "username": watcher.getName()})
            return
        room = watcher.getRoom()
        if room is None or not isinstance(payload, dict):
            return
        rawDomains = payload.get("domains")
        if not isinstance(rawDomains, list):
            return
        domains = []
        for entry in rawDomains:
            if not isinstance(entry, str):
                continue
            entry = entry.strip().lower()[:constants.TRUSTED_DOMAINS_MAX_LENGTH]
            if entry and entry not in domains:
                domains.append(entry)
            if len(domains) >= constants.TRUSTED_DOMAINS_MAX_COUNT:
                break
        if not domains:
            watcher.sendChatMessage({"message": getMessage("domains-empty-chat-message"),
                                     "username": watcher.getName()})
            return
        proposal = {"domains": domains, "by": watcher.getName()}
        room.setTrustedDomains(proposal)
        for receiver in room.getWatchers():
            self._sendTrustedDomainsToWatcher(receiver, proposal)
        watcher.sendChatMessage({"message": getMessage("domains-published-chat-message").format(len(domains)),
                                 "username": watcher.getName()})

    def _sendTrustedDomainsToWatcher(self, receiver, proposal):
        if receiver.supportsFeature("trustedDomains"):
            receiver.sendTrustedDomains(proposal)
        else:
            message = getMessage("domains-shared-chat-message").format(
                proposal.get("by", ""), ", ".join(proposal.get("domains", [])))
            receiver.sendChatMessage({"message": truncateText(message, self.maxChatMessageLength),
                                     "username": proposal.get("by", "")})

    def setTrackProposal(self, watcher, payload):
        # Admin publishes their current audio/sub selection as the room's recommended default.
        # Capable clients get Set:trackProposal (applied lua-side on layout match); others get chat.
        if not watcher.isAdmin():
            watcher.sendChatMessage({"message": getMessage("track-proposal-unauthorised-chat-message"),
                                     "username": watcher.getName()})
            return
        room = watcher.getRoom()
        if room is None or not isinstance(payload, dict):
            return
        proposal = {"by": watcher.getName()}
        for idKey, nameKey in (("audioId", "audioName"), ("subId", "subName")):
            value = payload.get(idKey)
            if value == "no" or (isinstance(value, int) and not isinstance(value, bool)
                                 and 1 <= value <= constants.TRACK_PROPOSAL_MAX_ID):
                proposal[idKey] = value
            name = payload.get(nameKey)
            if isinstance(name, str) and name:
                proposal[nameKey] = truncateText(name, constants.TRACK_PROPOSAL_MAX_NAME_LENGTH)
        signature = payload.get("signature")
        if isinstance(signature, str) and signature:
            proposal["signature"] = signature[:constants.TRACK_PROPOSAL_MAX_SIGNATURE_LENGTH]
        if "audioId" not in proposal and "subId" not in proposal:
            return  # nothing usable to recommend
        room.setTrackProposal(proposal)  # "latest" pointer (fallback chat, description, immediate apply)
        self._cacheTrackProposal(room.getName(), proposal)  # remember this layout for later matching files
        chatText = self._trackProposalChatText(proposal)
        for receiver in room.getWatchers():
            self._sendTrackProposalToWatcher(receiver, proposal, chatText)
        watcher.sendChatMessage({"message": getMessage("track-proposal-published-chat-message"),
                                 "username": watcher.getName()})

    def _cacheTrackProposal(self, roomName, proposal):
        # Store the proposal keyed by its layout signature so a later file with the same layout can
        # auto-apply it. No signature -> nothing to key on (uncached; still works as the "latest").
        signature = proposal.get("signature")
        if not signature:
            return
        cache = self._trackCache.setdefault(roomName, {})
        cache.pop(signature, None)  # re-insert so the newest keys sort last for FIFO eviction
        cache[signature] = proposal
        while len(cache) > constants.TRACK_CACHE_MAX_ENTRIES:
            del cache[next(iter(cache))]  # evict the oldest layout

    def _cachedTrackProposals(self, roomName):
        return list(self._trackCache.get(roomName, {}).values())

    @staticmethod
    def _trackProposalChatText(proposal):
        def describe(idKey, nameKey):
            if proposal.get(nameKey):
                return proposal[nameKey]
            value = proposal.get(idKey)
            if value == "no":
                return "off"
            return "#{}".format(value) if value is not None else "-"
        return getMessage("track-proposal-chat-message").format(
            proposal.get("by", ""), describe("audioId", "audioName"), describe("subId", "subName"))

    def _sendTrackProposalToWatcher(self, receiver, proposal, chatText=None):
        if receiver.supportsFeature("trackProposals"):
            receiver.sendTrackProposal(proposal)
            return
        # Fallback clients with no file yet are reminded when their first file loads instead
        # (see _remindTrackProposalOnFileChange) - avoids a stale-then-duplicate message.
        fileName = self._watcherFileName(receiver)
        if fileName is None:
            return
        receiver._lastTrackProposalAnnouncedFile = fileName
        receiver.sendChatMessage({"message": chatText if chatText else self._trackProposalChatText(proposal),
                                 "username": proposal.get("by", "")})

    @staticmethod
    def _watcherFileName(watcher):
        file_ = watcher.getFile()
        if isinstance(file_, dict):
            return file_.get("name")
        return file_ if file_ else None

    def _remindTrackProposalOnFileChange(self, watcher):
        # Per-watcher: whenever a fallback client's own file changes, re-post the recommendation
        # to that watcher (capable clients re-apply locally on file-loaded - no traffic needed).
        room = watcher.getRoom()
        proposal = room.getTrackProposal() if room is not None else None
        if proposal is None or watcher.supportsFeature("trackProposals"):
            return
        fileName = self._watcherFileName(watcher)
        if fileName is None or fileName == watcher._lastTrackProposalAnnouncedFile:
            return
        watcher._lastTrackProposalAnnouncedFile = fileName
        watcher.sendChatMessage({"message": self._trackProposalChatText(proposal),
                                 "username": proposal.get("by", "")})

    def _handleOSDChatCommand(self, watcher, message):
        if not watcher.isController():
            watcher.sendChatMessage({"message": getMessage("osd-command-unauthorised-chat-message"),
                                     "username": watcher.getName()})
            return
        options, text = self._parseOSDCommand(message)
        if not text:
            watcher.sendChatMessage({"message": getMessage("osd-command-usage-chat-message"),
                                     "username": watcher.getName()})
            return
        self.sendOSDMessage(watcher.getRoom(), text, watcher.getName(), **options)

    def updateYapTimer(self, room, paused, watcher):
        # Called on a genuine room pause/unpause. Accumulates pause time and, for clients that
        # cannot render the live overlay, announces it via chat (skipped for "yapTimer" clients).
        if not self.yapTimer or room is None:
            return
        room.yapResetIfFileChanged(self._getRoomFileKey(room))
        if paused:
            room.yapStartPause(watcher.getName())
            self._broadcastYapToRoom(room, watcher.getName(), getMessage("yap-timer-paused-chat-message"))
            self._startYapTicker(room)
        else:
            elapsed = room.yapEndPause()
            self._stopYapTicker(room)
            if elapsed is not None:
                total = room.yapTotal()
                afk = room.yapAfkTotal()
                self._broadcastYapToRoom(
                    room, watcher.getName(),
                    getMessage("yap-timer-unpaused-chat-message").format(
                        formatTime(elapsed), formatTime(total), formatTime(total - afk), formatTime(afk)))

    def _getRoomFileKey(self, room):
        # A value that changes when the room's current file changes, so the total can reset.
        index = room.getPlaylistIndex()
        playlist = room.getPlaylist()
        if index is not None and playlist and 0 <= index < len(playlist):
            return "index:{}:{}".format(index, playlist[index])
        setBy = room.getSetBy()
        file_ = setBy.getFile() if setBy else None
        if isinstance(file_, dict):
            return "file:{}".format(file_.get("name", ""))
        if file_:
            return "file:{}".format(file_)
        return None

    def _yapNoteFileChange(self, room):
        if self.yapTimer and room is not None:
            room.yapResetIfFileChanged(self._getRoomFileKey(room))

    def _yapNoteRewind(self, room, position):
        # A controller seeked the room back to (near) the start: reset the yap timer for the file.
        if self.yapTimer and room is not None and position is not None \
                and position <= constants.YAP_TIMER_REWIND_RESET_POSITION:
            room.yapResetOnRewind()

    def _yapNoteAfkPresence(self, room):
        # Tell the room's yap timer that its AFK presence may have changed, so the current
        # pause is split into "AFK" vs "active" time correctly.
        if self.yapTimer and room is not None:
            room.yapNoteAfkPresence(room.hasAfkWatcher())

    def _broadcastYapToRoom(self, room, username, message):
        messageDict = {"message": message, "username": username}
        for receiver in room.getWatchers():
            receiver.sendChatMessage(messageDict, skipIfSupportsFeature="yapTimer")

    def _startYapTicker(self, room):
        self._stopYapTicker(room)
        room._yapTickTimer = task.LoopingCall(self._yapTick, room)
        room._yapTickTimer.start(constants.YAP_TIMER_UPDATE_INTERVAL, now=False)

    def _stopYapTicker(self, room):
        if room._yapTickTimer is not None:
            if room._yapTickTimer.running:
                room._yapTickTimer.stop()
            room._yapTickTimer = None

    def _yapTick(self, room):
        if not room.isPaused() or room.isEmpty() or room.yapCheckExpired():
            self._stopYapTicker(room)
            return
        total = room.yapTotal()
        afk = room.yapAfkTotal()
        self._broadcastYapToRoom(
            room, room.yapPausedByName() or "",
            getMessage("yap-timer-ongoing-chat-message").format(
                formatTime(room.yapCurrentElapsed()), formatTime(total), formatTime(total - afk), formatTime(afk)))

    def updatePauseWarning(self, room, paused, watcher):
        # Warn the room when a single pause exceeds the threshold. Capable clients blink an OSD alert
        # (via state["pauseWarning"] in sendState); everyone else gets a periodic chat fallback.
        if not self.pauseWarningAfter or room is None:
            return
        if paused:
            room.yapStartPause(watcher.getName())  # shared pause clock (idempotent with the yap timer)
            self._startPauseWarningTimer(room)
        else:
            self._stopPauseWarningTimer(room)
            room.yapEndPause()

    def pauseWarningText(self, room):
        # Operator message, with an optional "{}" filled by the current pause duration (shown verbatim
        # if it has no placeholder, or if formatting the operator's text would fail).
        try:
            return self.pauseWarningMessage.format(formatTime(room.yapCurrentElapsed()))
        except (IndexError, KeyError, ValueError):
            return self.pauseWarningMessage

    def _startPauseWarningTimer(self, room):
        self._stopPauseWarningTimer(room)
        room._pauseWarningDelayed = reactor.callLater(self.pauseWarningAfter, self._firePauseWarning, room)

    def _firePauseWarning(self, room):
        room._pauseWarningDelayed = None
        if not room.isPaused() or room.isEmpty() or room.yapCheckExpired():
            return
        room._pauseWarningActive = True  # capable clients start blinking on the next State tick
        self._broadcastPauseWarningChat(room)
        room._pauseWarningTimer = task.LoopingCall(self._repeatPauseWarning, room)
        room._pauseWarningTimer.start(self.pauseWarningInterval, now=False)

    def _repeatPauseWarning(self, room):
        if not room.isPaused() or room.isEmpty() or room.yapCheckExpired():
            self._stopPauseWarningTimer(room)
            return
        self._broadcastPauseWarningChat(room)

    def _stopPauseWarningTimer(self, room):
        room._pauseWarningActive = False
        if room._pauseWarningDelayed is not None:
            if room._pauseWarningDelayed.active():
                room._pauseWarningDelayed.cancel()
            room._pauseWarningDelayed = None
        if room._pauseWarningTimer is not None:
            if room._pauseWarningTimer.running:
                room._pauseWarningTimer.stop()
            room._pauseWarningTimer = None

    def _broadcastPauseWarningChat(self, room):
        if room.hasAfkWatcher():
            return  # the room is knowingly waiting for someone - warning resumes once they return
        messageDict = {"message": self.pauseWarningText(room), "username": room.yapPausedByName() or ""}
        for receiver in room.getWatchers():
            receiver.sendChatMessage(messageDict, skipIfSupportsFeature="pauseWarning")

    def setAfk(self, watcher, isAfk):
        # AFK is per-connection like admin status. Going AFK also forces not-ready (directly, not
        # via setReady - its manual-change hook would instantly clear the AFK state again);
        # returning does not auto-restore ready.
        isAfk = bool(isAfk)
        if watcher.isAfk() == isAfk:
            return
        watcher.setAfk(isAfk)
        room = watcher.getRoom()
        if room is None:
            return
        messageKey = "afk-on-chat-message" if isAfk else "afk-off-chat-message"
        messageDict = {"message": getMessage(messageKey), "username": watcher.getName()}
        for receiver in room.getWatchers():
            if receiver.supportsFeature("afk"):
                receiver.sendSetAfk(watcher.getName(), isAfk)
            else:
                receiver.sendChatMessage(messageDict)
        if isAfk and not self.disableReady and watcher.isReady() is not False:
            watcher.setReady(False)
            self._roomManager.broadcastRoom(watcher, lambda w: w.sendSetReady(watcher.getName(), False, False))
        self._yapNoteAfkPresence(room)  # AFK presence flipped: split the current pause accordingly

    def _broadcastAfkToRoom(self, watcher):
        # Join/room-switch resync (mirrors the sendSetReady rebroadcast): fixes stale AFK views
        # held by destination-room members from an earlier shared-room stint.
        self._roomManager.broadcastRoom(
            watcher, lambda w: w.sendSetAfk(watcher.getName(), watcher.isAfk()) if w.supportsFeature("afk") else None)

    def setReady(self, watcher, isReady, manuallyInitiated=True, username=None):
        if username and username != watcher.getName():
            room = watcher.getRoom()
            if room.canControl(watcher):
                for watcherToSet in room.getWatchers():
                    if watcherToSet.getName() == username:
                        watcherToSet.setReady(isReady)
                        self._roomManager.broadcastRoom(watcher, lambda w: w.sendSetReady(watcherToSet.getName(), watcherToSet.isReady(), manuallyInitiated, watcher.getName()))
                        if isReady:
                            messageDict = { "message": getMessage("ready-chat-message").format(username), "username": watcher.getName()}
                        else:
                            messageDict = {"message": getMessage("not-ready-chat-message").format(username), "username": watcher.getName()}
                        self._roomManager.broadcastRoom(watcher, lambda w: w.sendChatMessage(messageDict, "setOthersReadiness"))
                        if isReady:
                            self.setAfk(watcherToSet, False)  # "ready but AFK" would contradict itself
        else:
            if manuallyInitiated and bool(isReady) != bool(watcher.isReady()):
                self.setAfk(watcher, False)  # deliberately changing readiness is activity
            watcher.setReady(isReady)
            self._roomManager.broadcastRoom(watcher, lambda w: w.sendSetReady(watcher.getName(), watcher.isReady(), manuallyInitiated))

    def setPlaylist(self, watcher, files):
        room = watcher.getRoom()
        if room.canControl(watcher) and playlistIsValid(files):
            watcher.getRoom().setPlaylist(files, watcher)
            self._roomManager.broadcastRoom(watcher, lambda w: w.setPlaylist(watcher.getName(), files))
        else:
            watcher.setPlaylist(room.getName(), room.getPlaylist())
            watcher.setPlaylistIndex(room.getName(), room.getPlaylistIndex())

    def setPlaylistIndex(self, watcher, index):
        room = watcher.getRoom()
        if room.canControl(watcher):
            watcher.getRoom().setPlaylistIndex(index, watcher)
            self._roomManager.broadcastRoom(watcher, lambda w: w.setPlaylistIndex(watcher.getName(), index))
            self._yapNoteFileChange(room)
        else:
            watcher.setPlaylistIndex(room.getName(), room.getPlaylistIndex())

    def _allowTLSconnections(self, path):
        try:
            privKey = open(path+'/privkey.pem', 'rb').read()
            certif = open(path+'/cert.pem', 'rb').read()
            chain = open(path+'/chain.pem', 'rb').read()

            self.lastEditCertTime = os.path.getmtime(path+'/cert.pem')

            privKeyPySSL = crypto.load_privatekey(crypto.FILETYPE_PEM, privKey)
            certifPySSL = crypto.load_certificate(crypto.FILETYPE_PEM, certif)

            sentinel = b'-----BEGIN CERTIFICATE-----'
            chainPySSL = [crypto.load_certificate(crypto.FILETYPE_PEM, sentinel + chain_cert) for chain_cert in
                          chain.split(sentinel)[1:]]

            cipherListString = "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"\
                               "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"\
                               "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384"
            accCiphers = ssl.AcceptableCiphers.fromOpenSSLCipherString(cipherListString)

            try:
                contextFactory = ssl.CertificateOptions(privateKey=privKeyPySSL, certificate=certifPySSL,
                                                        extraCertChain=chainPySSL, acceptableCiphers=accCiphers,
                                                        raiseMinimumTo=ssl.TLSVersion.TLSv1_2)
            except AttributeError:
                contextFactory = ssl.CertificateOptions(privateKey=privKeyPySSL, certificate=certifPySSL,
                                                        extraCertChain=chainPySSL, acceptableCiphers=accCiphers,
                                                        method=TLSv1_2_METHOD)

            self.options = contextFactory
            self.serverAcceptsTLS = True
            self._TLSattempts = 0
            print("TLS support is enabled.")
        except Exception as e:
            self.options = None
            self.serverAcceptsTLS = False
            self.lastEditCertTime = None
            print("Error while loading the TLS certificates.")
            print(e)
            print("TLS support is not enabled.")

    def checkLastEditCertTime(self):
        try:
            outTime = os.path.getmtime(self.certPath+'/cert.pem')
        except:
            outTime = None
        return outTime

    def updateTLSContextFactory(self):
        self._allowTLSconnections(self.certPath)
        self._TLSattempts += 1
        if self._TLSattempts < constants.TLS_CERT_ROTATION_MAX_RETRIES:
            self.serverAcceptsTLS = True


class StatsRecorder(object):
    def __init__(self, dbHandle, roomManager):
        self._dbHandle = dbHandle
        self._roomManagerHandle = roomManager

    def startRecorder(self, delay):
        try:
            self._dbHandle.connect()
            reactor.callLater(delay, self._scheduleClientSnapshot)
        except:
            print("--- Error in initializing the stats database. Server Stats not enabled. ---")

    def _scheduleClientSnapshot(self):
        self._clientSnapshotTimer = task.LoopingCall(self._runClientSnapshot)
        self._clientSnapshotTimer.start(constants.SERVER_STATS_SNAPSHOT_INTERVAL)

    def _runClientSnapshot(self):
        try:
            snapshotTime = int(time.time())
            rooms = self._roomManagerHandle.exportRooms()
            for room in rooms.values():
                for watcher in room.getWatchers():
                    self._dbHandle.addVersionLog(snapshotTime, watcher.getVersion())
        except:
            pass

class RoomsRecorder(StatsRecorder):
    def __init__(self, dbHandle, roomManager):
        self._dbHandle = dbHandle
        self._roomManagerHandle = roomManager

    def startRecorder(self, delay):
        try:
            self._dbHandle.connect()
            reactor.callLater(delay, self._scheduleClientSnapshot) # TODO: FIX THIS!
        except:
            print("--- Error in initializing the stats database. Server Stats not enabled. ---")

    def _scheduleClientSnapshot(self):
        self._clientSnapshotTimer = task.LoopingCall(self._runClientSnapshot)
        self._clientSnapshotTimer.start(constants.SERVER_STATS_SNAPSHOT_INTERVAL)

    def _runClientSnapshot(self):
        try:
            snapshotTime = int(time.time())
            rooms = self._roomManagerHandle.exportRooms()
            for room in rooms.values():
                for watcher in room.getWatchers():
                    self._dbHandle.addVersionLog(snapshotTime, watcher.getVersion())
        except:
            pass

class StatsDBManager(object):
    def __init__(self, dbpath):
        self._dbPath = dbpath
        self._connection = None

    def __del__(self):
        if self._connection is not None:
            self._connection.close()

    def connect(self):
        self._connection = adbapi.ConnectionPool("sqlite3", self._dbPath, check_same_thread=False)
        self._createSchema()

    def _createSchema(self):
        initQuery = 'create table if not exists clients_snapshots (snapshot_time INTEGER, version STRING)'
        return self._connection.runQuery(initQuery)

    def addVersionLog(self, timestamp, version):
        content = (timestamp, version, )
        self._connection.runQuery("INSERT INTO clients_snapshots VALUES (?, ?)", content)

class RoomDBManager(object):
    def __init__(self, dbpath, loadroomscallback):
        self._dbPath = dbpath
        self._connection = None
        self._loadRoomsCallback = loadroomscallback

    def __del__(self):
        if self._connection is not None:
            self._connection.close()

    def connect(self):
        self._connection = adbapi.ConnectionPool("sqlite3", self._dbPath, check_same_thread=False)
        self._createSchema().addCallback(self.loadRooms)

    def _createSchema(self):
        initQuery = 'create table if not exists persistent_rooms (name STRING PRIMARY KEY, playlist STRING, playlistIndex INTEGER, position REAL, lastSavedUpdate INTEGER)'
        return self._connection.runQuery(initQuery)

    def saveRoom(self, name, playlist, playlistIndex, position, lastUpdate):
        content = (name, playlist, playlistIndex, position, lastUpdate)
        self._connection.runQuery("INSERT OR REPLACE INTO persistent_rooms VALUES (?, ?, ?, ?, ?)", content)

    def deleteRoom(self, name):
        self._connection.runQuery("DELETE FROM persistent_rooms where name = ?", [name])

    def loadRooms(self, result=None):
        roomsQuery = "SELECT * FROM persistent_rooms"
        rooms = self._connection.runQuery(roomsQuery)
        rooms.addCallback(self.loadedRooms)

    def loadedRooms(self, rooms):
        self._loadRoomsCallback(rooms)

class RoomManager(object):
    def __init__(self, roomsdbfile=None, permanentRooms=[]):
        self._roomsDbFile = roomsdbfile
        self._rooms = {}
        self._permanentRooms = permanentRooms
        if self._roomsDbFile is not None:
            self._roomsDbHandle = RoomDBManager(self._roomsDbFile, self.loadRooms)
            self._roomsDbHandle.connect()
        else:
            self._roomsDbHandle = None

    def loadRooms(self, rooms):
        roomsLoaded = []
        for roomDetails in rooms:
            roomName = truncateText(roomDetails[0], constants.MAX_ROOM_NAME_LENGTH)
            room = Room(roomDetails[0], self._roomsDbHandle)
            room.loadRoom(roomDetails)
            if roomName in self._permanentRooms:
                room.setPermanent(True)
            self._rooms[roomName] = room
            roomsLoaded.append(roomName)
        for roomName in self._permanentRooms:
            if roomName not in roomsLoaded:
                roomDetails = (roomName, "", 0, 0, 0)
                room = Room(roomName, self._roomsDbHandle)
                room.loadRoom(roomDetails)
                room.setPermanent(True)
                self._rooms[roomName] = room

    def broadcastRoom(self, sender, whatLambda):
        room = sender.getRoom()
        if room and room.getName() in self._rooms:
            for receiver in room.getWatchers():
                whatLambda(receiver)

    def broadcast(self, sender, whatLambda):
        for room in self._rooms.values():
            for receiver in room.getWatchers():
                whatLambda(receiver)

    def getAllWatchersForUser(self, sender):
        watchers = []
        for room in self._rooms.values():
            for watcher in room.getWatchers():
                watchers.append(watcher)
        return watchers

    def getPersistentRooms(self, sender):
        persistentRooms = []
        for room in self._rooms.values():
            if room.isPersistent():
                persistentRooms.append(room.getName())
        return persistentRooms

    def getEmptyPersistentRooms(self):
        emptyPersistentRooms = []
        for room in self._rooms.values():
            if len(room.getWatchers()) == 0:
                emptyPersistentRooms.append(room.getName())
        return emptyPersistentRooms

    def moveWatcher(self, watcher, roomName):
        roomName = truncateText(roomName, constants.MAX_ROOM_NAME_LENGTH)
        self.removeWatcher(watcher)
        room = self._getRoom(roomName)
        room.addWatcher(watcher)

    def removeWatcher(self, watcher):
        oldRoom = watcher.getRoom()
        if oldRoom:
            oldRoom.removeWatcher(watcher)
            self._deleteRoomIfEmpty(oldRoom)

    def _getRoom(self, roomName):
        if roomName in self._rooms:
            return self._rooms[roomName]
        else:
            if RoomPasswordProvider.isControlledRoom(roomName):
                room = ControlledRoom(roomName, self._roomsDbHandle)
            else:
                room = Room(roomName, self._roomsDbHandle)
            self._rooms[roomName] = room
            return room

    def _deleteRoomIfEmpty(self, room):
        if room.isPermanent():
            return
        if room.isPersistent() and not room.isPlaylistEmpty():
            return
        if room.isEmpty() and room.getName():
            del self._rooms[room.getName()]

    def findFreeUsername(self, username, maxUsernameLength=constants.MAX_USERNAME_LENGTH):
        username = truncateText(username, maxUsernameLength)
        allnames = []
        for room in self._rooms.values():
            for watcher in room.getWatchers():
                allnames.append(watcher.getName().lower())
        if username.lower() in allnames and username.endswith('_'):
            username = username.rstrip('_') or '_'
        while username.lower() in allnames:
            username += '_'
        return username

    def exportRooms(self):
        return self._rooms


class PublicRoomManager(RoomManager):
    def broadcast(self, sender, what):
        self.broadcastRoom(sender, what)

    def getAllWatchersForUser(self, sender):
        return sender.getRoom().getWatchers()

    def moveWatcher(self, watcher, room):
        oldRoom = watcher.getRoom()
        l = lambda w: w.sendSetting(watcher.getName(), oldRoom, None, {"left": True})
        self.broadcast(watcher, l)
        RoomManager.moveWatcher(self, watcher, room)
        watcher.setFile(watcher.getFile())


class Room(object):
    STATE_PAUSED = 0
    STATE_PLAYING = 1

    def __init__(self, name, roomsdbhandle):
        self._name = name
        self._roomsDbHandle = roomsdbhandle
        self._watchers = {}
        self._playState = self.STATE_PAUSED
        self._setBy = None
        self._playlist = []
        self._playlistIndex = None
        self._lastUpdate = time.time()
        self._lastSavedUpdate = 0
        self._position = 0
        self._permanent = False
        self._yapPauseStartedAt = None  # Wall-clock time the current pause began (None while playing)
        self._yapTotalThisFile = 0.0  # Accumulated duration of completed pauses for the current file
        self._yapAfkTotalThisFile = 0.0  # Of that total, the portion spent with an AFK watcher present (active = total - afk)
        self._yapAfkAccumThisPause = 0.0  # Closed AFK segments within the current pause (folded into the file total on unpause)
        self._yapAfkSegStartedAt = None  # Start of the currently-open AFK segment (None when no AFK watcher, or not paused)
        self._yapCurrentFileKey = None  # Identifies the current file; total resets when this changes
        self._yapPausedByName = None  # Username that started the current pause (for chat attribution)
        self._yapTickTimer = None  # LoopingCall broadcasting periodic "still paused" updates
        self._pauseWarningDelayed = None  # DelayedCall for the first pause warning (at the threshold)
        self._pauseWarningTimer = None  # LoopingCall re-reminding (chat fallback) while over the threshold
        self._pauseWarningActive = False  # True while the current pause is over the threshold (drives the blinking OSD)
        self._yapExpired = False  # True once the current pause exceeds YAP_TIMER_MAX_PAUSE; everything stays quiet until the next pause
        self._locked = False  # Locked by a server admin: only admins control playback/playlist. Runtime-only
        self._trackProposal = None  # Admin-recommended default audio/sub tracks (dict). Runtime-only
        self._trustedDomains = None  # Admin-published trusted domains for the room (dict). Runtime-only

    def __str__(self, *args, **kwargs):
        return self.getName()

    def roomsCanPersist(self):
        return self._roomsDbHandle is not None

    def isPersistent(self):
        return self.roomsCanPersist() and not self.isMarkedAsTemporary()

    def isMarkedAsTemporary(self):
        roomName = self.getName().lower()
        return roomName.endswith("-temp") or "-temp:" in roomName

    def isPlaylistEmpty(self):
        return len(self._playlist) == 0

    def isPermanent(self):
        return self._permanent

    def isNotPermanent(self):
        return not self.isPermanent()

    def sanitizeFilename(self, filename, blacklist="<>:/\\|?*\"", placeholder="_"):
        return ''.join([c if c not in blacklist and ord(c) >= 32 else placeholder for c in filename])

    def writeToDb(self):
        if not self.isPersistent():
            return
        if self.isPlaylistEmpty():
            self._roomsDbHandle.deleteRoom(self._name)
        else:
            processed_playlist = getListAsMultilineString(self._playlist)
            self._roomsDbHandle.saveRoom(self._name, processed_playlist, self._playlistIndex, self._position, self._lastSavedUpdate)

    def loadRoom(self, room):
        name, playlist, playlistindex, position, lastupdate = room
        self._name = name
        self._playlist = convertMultilineStringToList(playlist)
        self._playlistIndex = playlistindex
        self._position = position
        self._lastSavedUpdate = lastupdate

    def getName(self):
        return self._name

    def getPosition(self):
        age = time.time() - self._lastUpdate
        referenceWatchers = self._watchers
        if self._locked:
            # Locked room: only server admins are a valid position reference
            referenceWatchers = {name: w for name, w in self._watchers.items() if w.isAdmin()}
        if referenceWatchers and age > 1:
            watcher = min(referenceWatchers.values())
            self._setBy = watcher
            self._position = watcher.getPosition()
            self._lastSavedUpdate = self._lastUpdate = time.time()
            return self._position
        elif self._position is not None:
            return self._position + (age if self._playState == self.STATE_PLAYING else 0)
        else:
            return 0

    def setPaused(self, paused=STATE_PAUSED, setBy=None):
        if not self.canControl(setBy):
            return
        self._playState = paused
        self._setBy = setBy
        self.writeToDb()

    def setPosition(self, position, setBy=None):
        if not self.canControl(setBy):
            return
        self._position = position
        for watcher in self._watchers.values():
            watcher.setPosition(position)
            self._setBy = setBy
        self.writeToDb()

    def setPermanent(self, newState):
        self._permanent = newState

    def isPlaying(self):
        return self._playState == self.STATE_PLAYING

    def isPaused(self):
        return self._playState == self.STATE_PAUSED

    def yapStartPause(self, pausedByName=None):
        if self._yapPauseStartedAt is None:
            self._yapPauseStartedAt = time.time()
            self._yapPausedByName = pausedByName
            self._yapExpired = False  # a new pause starts with a clean slate
            # Split the pause into "AFK" vs "active" segments: an AFK segment is open whenever
            # the room has an AFK watcher. Seed it from the current presence.
            self._yapAfkAccumThisPause = 0.0
            self._yapAfkSegStartedAt = time.time() if self.hasAfkWatcher() else None

    def yapNoteAfkPresence(self, hasAfk):
        # Called whenever the room's AFK presence may have flipped. Opens/closes the current
        # AFK segment on the edges; a no-op unless a (non-expired) pause is actively timed.
        if self._yapPauseStartedAt is None or self._yapExpired:
            return
        if hasAfk and self._yapAfkSegStartedAt is None:
            self._yapAfkSegStartedAt = time.time()
        elif not hasAfk and self._yapAfkSegStartedAt is not None:
            self._yapCloseAfkSegment()

    def _yapCloseAfkSegment(self):
        if self._yapAfkSegStartedAt is not None:
            self._yapAfkAccumThisPause += time.time() - self._yapAfkSegStartedAt
            self._yapAfkSegStartedAt = None

    def yapEndPause(self):
        if self._yapPauseStartedAt is None:
            return None
        if self._yapExpired:
            # The pause outlived YAP_TIMER_MAX_PAUSE: discard it entirely (no accumulation,
            # and returning None suppresses the unpause summary).
            self._yapPauseStartedAt = None
            self._yapAfkSegStartedAt = None
            self._yapAfkAccumThisPause = 0.0
            return None
        self._yapCloseAfkSegment()
        elapsed = time.time() - self._yapPauseStartedAt
        self._yapTotalThisFile += elapsed
        self._yapAfkTotalThisFile += self._yapAfkAccumThisPause
        self._yapPauseStartedAt = None
        self._yapAfkAccumThisPause = 0.0
        return elapsed

    def yapCheckExpired(self):
        # Lazily trip the give-up state once a single pause exceeds the cap: wipe the per-file
        # total and stay quiet (no overlay/chat/summary) until the next pause rearms via
        # yapStartPause. The wall-clock start is kept so nothing mid-pause reads a bogus duration.
        if not self._yapExpired and self._yapPauseStartedAt is not None \
                and time.time() - self._yapPauseStartedAt >= constants.YAP_TIMER_MAX_PAUSE:
            self._yapExpired = True
            self._yapTotalThisFile = 0.0
            self._yapAfkTotalThisFile = 0.0
        return self._yapExpired

    def yapCurrentElapsed(self):
        if self._yapPauseStartedAt is None:
            return 0.0
        return time.time() - self._yapPauseStartedAt

    def yapAfkCurrentElapsed(self):
        # AFK time within the current pause: closed segments plus any open one.
        if self._yapPauseStartedAt is None:
            return 0.0
        openSeg = (time.time() - self._yapAfkSegStartedAt) if self._yapAfkSegStartedAt is not None else 0.0
        return self._yapAfkAccumThisPause + openSeg

    def yapTotal(self):
        return self._yapTotalThisFile + self.yapCurrentElapsed()

    def yapAfkTotal(self):
        return self._yapAfkTotalThisFile + self.yapAfkCurrentElapsed()

    def yapPausedByName(self):
        return self._yapPausedByName

    def yapReset(self):
        self._yapTotalThisFile = 0.0
        self._yapAfkTotalThisFile = 0.0
        self._yapAfkAccumThisPause = 0.0
        self._yapAfkSegStartedAt = None
        self._yapPauseStartedAt = None
        self._yapExpired = False

    def yapResetIfFileChanged(self, fileKey):
        if fileKey != self._yapCurrentFileKey:
            self.yapReset()
            self._yapCurrentFileKey = fileKey

    def yapResetOnRewind(self):
        # A rewind to the very start replays the file, so wipe the per-file totals exactly like a
        # file change (the file key is unchanged, so yapResetIfFileChanged never catches it). If the
        # room is still paused, re-arm a fresh pause clock from 00:00 so the current pause keeps
        # counting instead of freezing mid-pause.
        pausedByName = self._yapPausedByName
        self.yapReset()
        if self.isPaused():
            self.yapStartPause(pausedByName)

    def getWatchers(self):
        return list(self._watchers.values())

    def hasAfkWatcher(self):
        # Computed over live watchers so disconnects/room switches self-heal - no cleanup needed.
        return any(w.isAfk() for w in self.getWatchers())

    def addWatcher(self, watcher):
        if self._watchers or self.isPersistent():
            watcher.setPosition(self.getPosition())
        self._watchers[watcher.getName()] = watcher
        watcher.setRoom(self)

    def removeWatcher(self, watcher):
        if watcher.getName() not in self._watchers:
            return
        del self._watchers[watcher.getName()]
        watcher.setRoom(None)
        if not self._watchers and not self.isPersistent():
            self._position = 0
        self.writeToDb()

    def isEmpty(self):
        return not bool(self._watchers)

    def getSetBy(self):
        return self._setBy

    def canControl(self, watcher):
        # Plain rooms are free-for-all unless a server admin has locked them.
        return (not self._locked) or (watcher is not None and watcher.isAdmin())

    def isLocked(self):
        return self._locked

    def setLocked(self, locked):
        self._locked = locked

    def getTrackProposal(self):
        return self._trackProposal

    def setTrackProposal(self, proposal):
        self._trackProposal = proposal

    def getTrustedDomains(self):
        return self._trustedDomains

    def setTrustedDomains(self, payload):
        self._trustedDomains = payload

    def setPlaylist(self, files, setBy=None):
        if self.canControl(setBy):
            self._playlist = files
            self.writeToDb()

    def setPlaylistIndex(self, index, setBy=None):
        if self.canControl(setBy):
            self._playlistIndex = index
            self.writeToDb()

    def getPlaylist(self):
        return self._playlist

    def getPlaylistIndex(self):
        return self._playlistIndex

    def getControllers(self):
        return []

class ControlledRoom(Room):
    def __init__(self, name, roomsdbhandle):
        Room.__init__(self, name, roomsdbhandle)
        self._controllers = {}

    def getPosition(self):
        age = time.time() - self._lastUpdate
        referenceWatchers = dict(self._controllers)
        for name, watcher in self._watchers.items():
            if watcher.isAdmin():
                referenceWatchers[name] = watcher  # admins are implicit controllers
        if referenceWatchers and age > 1:
            watcher = min(referenceWatchers.values())
            self._setBy = watcher
            self._position = watcher.getPosition()
            self._lastUpdate = time.time()
            return self._position
        elif self._position is not None:
            return self._position + (age if self._playState == self.STATE_PLAYING else 0)
        else:
            return 0

    def addController(self, watcher):
        self._controllers[watcher.getName()] = watcher

    def removeWatcher(self, watcher):
        Room.removeWatcher(self, watcher)
        if watcher.getName() in self._controllers:
            del self._controllers[watcher.getName()]
        self.writeToDb()

    def setPaused(self, paused=Room.STATE_PAUSED, setBy=None):
        if self.canControl(setBy):
            Room.setPaused(self, paused, setBy)

    def setPosition(self, position, setBy=None):
        if self.canControl(setBy):
            Room.setPosition(self, position, setBy)

    def setPlaylist(self, files, setBy=None):
        if self.canControl(setBy) and playlistIsValid(files):
            self._playlist = files

    def setPlaylistIndex(self, index, setBy=None):
        if self.canControl(setBy):
            self._playlistIndex = index

    def canControl(self, watcher):
        if watcher is None:
            return False
        return watcher.isAdmin() or watcher.getName() in self._controllers

    def getControllers(self):
        return {}


class Watcher(object):
    def __init__(self, server, connector, name):
        self._ready = None
        self._server = server
        self._connector = connector
        self._name = name
        self._isAdmin = False
        self._isAfk = False
        self._lastTrackProposalAnnouncedFile = None
        self._room = None
        self._file = None
        self._position = None
        self._lastUpdatedOn = time.time()
        self._sendStateTimer = None
        self._connector.setWatcher(self)
        reactor.callLater(0.1, self._scheduleSendState)

    def setFile(self, file_):
        if file_ and "name" in file_:
            file_["name"] = truncateText(file_["name"], constants.MAX_FILENAME_LENGTH)
        self._file = file_
        self._server.sendFileUpdate(self)

    def setRoom(self, room):
        self._room = room
        if room is None:
            self._deactivateStateTimer()
        else:
            self._resetStateTimer()
            self._askForStateUpdate(True, True)

    def setReady(self, ready):
        self._ready = ready

    def getFeatures(self):
        features = self._connector.getFeatures()
        return features

    def isReady(self):
        if self._server.disableReady:
            return None
        return self._ready

    def getRoom(self):
        return self._room

    def getName(self):
        return self._name

    def getVersion(self):
        return self._connector.getVersion()

    def getFile(self):
        return self._file

    def setPosition(self, position):
        self._position = position

    def getPosition(self):
        if self._position is None:
            return None
        if self._room.isPlaying():
            timePassedSinceSet = time.time() - self._lastUpdatedOn
        else:
            timePassedSinceSet = 0
        return self._position + timePassedSinceSet

    def sendSetting(self, user, room, file_, event):
        self._connector.sendUserSetting(user, room, file_, event)

    def sendNewControlledRoom(self, roomBaseName, password):
        self._connector.sendNewControlledRoom(roomBaseName, password)

    def sendControlledRoomAuthStatus(self, success, username, room):
        self._connector.sendControlledRoomAuthStatus(success, username, room)

    def sendChatMessage(self, message, skipIfSupportsFeature=None):
        if self._connector.meetsMinVersion(constants.CHAT_MIN_VERSION):
            if skipIfSupportsFeature and self.supportsFeature(skipIfSupportsFeature):
                return
            self._connector.sendMessage({"Chat": message})

    def sendOSDMessage(self, payload):
        self._connector.sendSet({"osdMessage": payload})

    def sendTrackProposal(self, payload):
        self._connector.sendSet({"trackProposal": payload})

    def sendTrustedDomains(self, payload):
        self._connector.sendSet({"trustedDomains": payload})

    def sendList(self, toGUIOnly=False):
        if toGUIOnly and self.isGUIUser(self._connector.getFeatures()):
            clientFeatures = self._connector.getFeatures()
            if "uiMode" in clientFeatures:
                if clientFeatures["uiMode"] == constants.CONSOLE_UI_MODE:
                    return
            else:
                return
        self._connector.sendList()

    def isGUIUser(self, clientFeatures):
        clientFeatures = self._connector.getFeatures()
        uiMode = clientFeatures["uiMode"] if "uiMode" in clientFeatures else constants.UNKNOWN_UI_MODE
        if uiMode == constants.UNKNOWN_UI_MODE:
            uiMode = constants.FALLBACK_ASSUMED_UI_MODE
        return uiMode == constants.GRAPHICAL_UI_MODE

    def supportsFeature(self, clientFeature):
        clientFeatures = self._connector.getFeatures()
        return clientFeatures[clientFeature] if clientFeature in clientFeatures else False

    def sendSetReady(self, username, isReady, manuallyInitiated=True, setByUsername=None):
        self._connector.sendSetReady(username, isReady, manuallyInitiated, setByUsername)

    def sendSetAfk(self, username, isAfk):
        self._connector.sendSetAfk(username, isAfk)

    def setPlaylistIndex(self, username, index):
        self._connector.setPlaylistIndex(username, index)

    def setPlaylist(self, username, files):
        self._connector.setPlaylist(username, files)

    def __lt__(self, b):
        if self.getPosition() is None or self._file is None:
            return False
        if b.getPosition() is None or b.getFile() is None:
            return True
        return self.getPosition() < b.getPosition()

    def _scheduleSendState(self):
        self._sendStateTimer = task.LoopingCall(self._askForStateUpdate)
        self._sendStateTimer.start(constants.SERVER_STATE_INTERVAL)

    def _askForStateUpdate(self, doSeek=False, forcedUpdate=False):
        self._server.sendState(self, doSeek, forcedUpdate)

    def _resetStateTimer(self):
        if self._sendStateTimer:
            if self._sendStateTimer.running:
                self._sendStateTimer.stop()
            self._sendStateTimer.start(constants.SERVER_STATE_INTERVAL)

    def _deactivateStateTimer(self):
        if self._sendStateTimer and self._sendStateTimer.running:
            self._sendStateTimer.stop()

    def sendState(self, position, paused, doSeek, setBy, forcedUpdate):
        if self._connector.isLogged():
            self._connector.sendState(position, paused, doSeek, setBy, forcedUpdate)
        if time.time() - self._lastUpdatedOn > constants.PROTOCOL_TIMEOUT:
            self._server.removeWatcher(self)
            self._connector.drop()

    def __hasPauseChanged(self, paused):
        if paused is None:
            return False
        return self._room.isPaused() and not paused or not self._room.isPaused() and paused

    def _updatePositionByAge(self, messageAge, paused, position):
        if not paused:
            position += messageAge
        return position

    def updateState(self, position, paused, doSeek, messageAge):
        pauseChanged = self.__hasPauseChanged(paused)
        self._lastUpdatedOn = time.time()
        if ((pauseChanged and not paused) or doSeek) and self._isAfk:
            # Returning to active watching clears AFK: unpausing or seeking - even a
            # non-controller's attempt that is about to be reverted. Pausing does NOT
            # clear it (stepping away is the whole point, and the AFK keybind pauses on
            # the way out). Echoes of server-forced changes never reach here
            # (ignoring-on-the-fly gate + __hasPauseChanged compares against room state).
            self._server.setAfk(self, False)
        if pauseChanged:
            self.getRoom().setPaused(Room.STATE_PAUSED if paused else Room.STATE_PLAYING, self)
            if self.getRoom().canControl(self):
                self._server.updateYapTimer(self.getRoom(), paused, self)
                self._server.updatePauseWarning(self.getRoom(), paused, self)
        if position is not None:
            position = self._updatePositionByAge(messageAge, paused, position)
            self.setPosition(position)
        if doSeek or pauseChanged:
            self._server.forcePositionUpdate(self, doSeek, paused)

    def isAdmin(self):
        return self._isAdmin

    def setAdmin(self, isAdmin):
        self._isAdmin = isAdmin

    def isAfk(self):
        return self._isAfk

    def setAfk(self, isAfk):
        self._isAfk = isAfk

    def isController(self):
        if self._isAdmin:
            return True
        return RoomPasswordProvider.isControlledRoom(self._room.getName()) \
            and self._room.canControl(self)


class ConfigurationGetter(object):
    def getConfiguration(self):
        self._prepareArgParser()
        args = self._argparser.parse_args()
        if args.port is None:
            args.port = constants.DEFAULT_PORT
        return args

    def _prepareArgParser(self):
        self._argparser = argparse.ArgumentParser(
            description=getMessage("server-argument-description"),
            epilog=getMessage("server-argument-epilog"))
        self._argparser.add_argument('--port', metavar='port', type=str, nargs='?', help=getMessage("server-port-argument"))
        self._argparser.add_argument('--password', metavar='password', type=str, nargs='?', help=getMessage("server-password-argument"), default=os.environ.get('SYNCPLAY_PASSWORD'))
        self._argparser.add_argument('--isolate-rooms', action='store_true', help=getMessage("server-isolate-room-argument"))
        self._argparser.add_argument('--disable-ready', action='store_true', help=getMessage("server-disable-ready-argument"))
        self._argparser.add_argument('--disable-chat', action='store_true', help=getMessage("server-chat-argument"))
        self._argparser.add_argument('--yap-timer', action='store_true', help=getMessage("server-yap-timer-argument"))
        self._argparser.add_argument('--pause-warning-after', metavar='seconds', type=int, nargs='?', help=getMessage("server-pause-warning-after-argument"))
        self._argparser.add_argument('--pause-warning-interval', metavar='seconds', type=int, nargs='?', help=getMessage("server-pause-warning-interval-argument"))
        self._argparser.add_argument('--pause-warning-message', metavar='message', type=str, nargs='?', help=getMessage("server-pause-warning-message-argument"))
        self._argparser.add_argument('--admin-password', metavar='adminPassword', type=str, nargs='?', help=getMessage("server-admin-password-argument"), default=os.environ.get('SYNCPLAY_ADMIN_PASSWORD'))
        self._argparser.add_argument('--salt', metavar='salt', type=str, nargs='?', help=getMessage("server-salt-argument"), default=os.environ.get('SYNCPLAY_SALT'))
        self._argparser.add_argument('--motd-file', metavar='file', type=str, nargs='?', help=getMessage("server-motd-argument"))
        self._argparser.add_argument('--rooms-db-file', metavar='rooms', type=str, nargs='?', help=getMessage("server-rooms-argument"))
        self._argparser.add_argument('--permanent-rooms-file', metavar='permanentrooms', type=str, nargs='?', help=getMessage("server-permanent-rooms-argument"))
        self._argparser.add_argument('--max-chat-message-length', metavar='maxChatMessageLength', type=int, nargs='?', help=getMessage("server-chat-maxchars-argument").format(constants.MAX_CHAT_MESSAGE_LENGTH))
        self._argparser.add_argument('--max-username-length', metavar='maxUsernameLength', type=int, nargs='?', help=getMessage("server-maxusernamelength-argument").format(constants.MAX_USERNAME_LENGTH))
        self._argparser.add_argument('--stats-db-file', metavar='file', type=str, nargs='?', help=getMessage("server-stats-db-file-argument"))
        self._argparser.add_argument('--tls', metavar='path', type=str, nargs='?', help=getMessage("server-startTLS-argument"))
        self._argparser.add_argument('--ipv4-only', action='store_true', help=getMessage("server-listen-only-on-ipv4"))
        self._argparser.add_argument('--ipv6-only', action='store_true', help=getMessage("server-listen-only-on-ipv6"))
        self._argparser.add_argument('--interface-ipv4', metavar='interfaceIPv4', type=str, nargs='?', help=getMessage("server-interface-ipv4"), default='')
        self._argparser.add_argument('--interface-ipv6', metavar='interfaceIPv6', type=str, nargs='?', help=getMessage("server-interface-ipv6"), default='')
