# coding:utf8
import json
import time
from datetime import datetime
from functools import wraps

from twisted import version as twistedVersion
from twisted.internet.interfaces import IHandshakeListener
from twisted.protocols.basic import LineReceiver
from twisted.python.versions import Version
from zope.interface.declarations import implementer

import syncplay
from syncplay import constants
from syncplay.constants import PING_MOVING_AVERAGE_WEIGHT, CONTROLLED_ROOMS_MIN_VERSION, USER_READY_MIN_VERSION, SHARED_PLAYLIST_MIN_VERSION, CHAT_MIN_VERSION, UNKNOWN_UI_MODE
from syncplay.messages import getMessage
from syncplay.utils import meetsMinVersion


class JSONCommandProtocol(LineReceiver):
    def handleMessages(self, messages):
        for message in messages.items():
            command = message[0]
            if command == "Hello":
                self.handleHello(message[1])
            elif command == "Set":
                self.handleSet(message[1])
            elif command == "List":
                self.handleList(message[1])
            elif command == "State":
                self.handleState(message[1])
            elif command == "Error":
                self.handleError(message[1])
            elif command == "Chat":
                self.handleChat(message[1])
            elif command == "TLS":
                self.handleTLS(message[1])
            else:
                self.dropWithError(getMessage("unknown-command-server-error").format(message[1]))  # TODO: log, not drop

    def lineReceived(self, line):
        try:
            line = line.decode('utf-8').strip()
        except UnicodeDecodeError:
            self.dropWithError(getMessage("line-decode-server-error"))
            return
        if not line:
            return
        self.showDebugMessage("client/server << {}".format(line))
        try:
            messages = json.loads(line)
        except json.decoder.JSONDecodeError:
            self.dropWithError(getMessage("not-json-server-error").format(line))
            return
        else:
            self.handleMessages(messages)

    def sendMessage(self, dict_):
        line = json.dumps(dict_)
        self.sendLine(line.encode('utf-8'))
        self.showDebugMessage("client/server >> {}".format(line))

    def drop(self):
        self.transport.loseConnection()

    def abort(self):
        if hasattr(self.transport, "abortConnection"):
            self.transport.abortConnection()
        else:
            self.drop()
    
    def dropWithError(self, error):
        raise NotImplementedError()


@implementer(IHandshakeListener)
class SyncClientProtocol(JSONCommandProtocol):
    def __init__(self, client):
        self._client = client
        self.clientIgnoringOnTheFly = 0
        self.serverIgnoringOnTheFly = 0
        self.logged = False
        self.hadFirstPlaylistIndex = False
        self.hadFirstStateUpdate = False
        self._sentBuffering = False  # Whether our last State claimed we were buffering (drives the one falling-edge report)
        self._pendingStateChange = False  # A user's pause/seek that could not be sent yet, held until the outstanding one is acknowledged
        self._pingService = PingService()

    def showDebugMessage(self, line):
        self._client.ui.showDebugMessage(line)

    def connectionMade(self):
        self.hadFirstPlaylistIndex = False
        self.hadFirstStateUpdate = False
        self._client.initProtocol(self)
        if self._client._clientSupportsTLS:
            if self._client._serverSupportsTLS:
                self.sendTLS({"startTLS": "send"})
                self._client.ui.showMessage(getMessage("startTLS-initiated"))
            else:
                self._client.ui.showErrorMessage(getMessage("startTLS-not-supported-server"))
                self.sendHello()
        else:
            self._client.ui.showMessage(getMessage("startTLS-not-supported-client"))
            self.sendHello()

    def connectionLost(self, reason):
        try:
            if "Invalid DNS-ID" in str(reason.value):
                self._client._serverSupportsTLS = False
            elif "tlsv1 alert protocol version" in str(reason.value):
                self._client._clientSupportsTLS = False
            elif "certificate verify failed" in str(reason.value):
                self.dropWithError(getMessage("startTLS-server-certificate-invalid"))
            elif "mismatched_id=DNS_ID" in str(reason.value):
                self.dropWithError(getMessage("startTLS-server-certificate-invalid-DNS-ID"))
            elif reason:
                try:
                    self._client.ui.showErrorMessage(str(type(reason)))
                    self._client.ui.showErrorMessage(str(reason))
                    if reason.stack:
                        self._client.ui.showErrorMessage(str(reason.stack))
                    self._client.ui.showErrorMessage(str(reason.value))
                except:
                    pass
        except:
            pass
        self._client.destroyProtocol()

    def dropWithError(self, error):
        self._client.ui.showErrorMessage(error)
        self._client.protocolFactory.stopRetrying()
        self.drop()

    def _extractHelloArguments(self, hello):
        username = hello["username"] if "username" in hello else None
        roomName = hello["room"]["name"] if "room" in hello else None
        version = hello["version"] if "version" in hello else None
        version = hello["realversion"] if "realversion" in hello else version  # Used for 1.2.X compatibility
        motd = hello["motd"] if "motd" in hello else None
        features = hello["features"] if "features" in hello else None
        return username, roomName, version, motd, features

    def handleHello(self, hello):
        username, roomName, version, motd, featureList = self._extractHelloArguments(hello)
        if not username or not roomName or not version:
            self.dropWithError(getMessage("hello-server-error").format(hello))
        else:
            self._client.setUsername(username)
            self._client.setRoom(roomName)
        self.logged = True
        if self.persistentRoomWarning(featureList):
            span = "\n\n" if motd else ""
            motd = getMessage("persistent-rooms-notice") + span + motd
        if motd:
            self._client.ui.showMessage(motd, noPlayer=True, noTimestamp=True, isMotd=True)
        self._client.ui.showMessage(getMessage("connected-successful-notification"))
        self._client.connected()
        self._client.sendFile()
        self._client.setServerVersion(version, featureList)

    def persistentRoomWarning(self, serverFeatures):
        return serverFeatures["persistentRooms"] if "persistentRooms" in serverFeatures else False

    def sendHello(self):
        hello = {}
        hello["username"] = self._client.getUsername()
        password = self._client.getPassword()
        if password:
            hello["password"] = password
        room = self._client.getRoom()
        if room:
            hello["room"] = {"name": room}
        hello["version"] = "1.2.255"  # Used so newer clients work on 1.2.X server
        hello["realversion"] = syncplay.version
        hello["features"] = self._client.getFeatures()
        self.sendMessage({"Hello": hello})

    def _SetUser(self, users):
        for user in users.items():
            username = user[0]
            settings = user[1]
            room = settings["room"]["name"] if "room" in settings else None
            file_ = settings["file"] if "file" in settings else None
            if "event" in settings:
                if "joined" in settings["event"]:
                    self._client.userlist.addUser(username, room, file_)
                elif "left" in settings["event"]:
                    self._client.removeUser(username)
            else:
                self._client.userlist.modUser(username, room, file_)

    def handleSet(self, settings):
        for (command, values) in settings.items():
            if command == "room":
                roomName = values["name"] if "name" in values else None
                self._client.setRoom(roomName)
            elif command == "user":
                self._SetUser(values)
            elif command == "controllerAuth":
                if values['success']:
                    self._client.controllerIdentificationSuccess(values["user"], values["room"])
                else:
                    self._client.controllerIdentificationError(values["user"], values["room"])
            elif command == "newControlledRoom":
                controlPassword = values['password']
                roomName = values['roomName']
                self._client.controlledRoomCreated(roomName, controlPassword)
            elif command == "ready":
                user, isReady = values["username"], values["isReady"]
                manuallyInitiated = values["manuallyInitiated"] if "manuallyInitiated" in values else True
                setBy = values["setBy"] if "setBy" in values else None
                self._client.setReady(user, isReady, manuallyInitiated, setBy)
            elif command == "playlistIndex":
                user = values['user']
                resetPosition = True
                if not self.hadFirstPlaylistIndex:
                    self.hadFirstPlaylistIndex = True
                    resetPosition = False
                self._client.playlist.changeToPlaylistIndex(values['index'], user, resetPosition=resetPosition)
            elif command == "playlistChange":
                self._client.playlist.changePlaylist(values['files'], values['user'])
            elif command == "features":
                self._client.setUserFeatures(values["username"], values['features'])
            elif command == "osdMessage":
                self._client.ui.showGenericOSD(values)
            elif command == "trackProposal":
                self._client.ui.setTrackProposal(values)
            elif command == "trustedDomains":
                self._client.setServerTrustedDomains(values)
            elif command == "afk":
                self._client.setAfk(values.get("username"), bool(values.get("isAfk")), values.get("setBy"))
            elif command == "roomLock":
                self._client.setRoomLocked(values.get("room"), bool(values.get("locked")), values.get("setBy"))

    def sendFeaturesUpdate(self, features):
        self.sendSet({"features": features})

    def sendAdminAuth(self, password):
        self.sendSet({"adminAuth": {"password": password}})

    def sendTrackProposal(self, payload):
        self.sendSet({"trackProposal": payload})

    def sendTrustedDomains(self, payload):
        self.sendSet({"trustedDomains": payload})

    def sendSet(self, setting):
        self.sendMessage({"Set": setting})

    def sendRoomSetting(self, roomName, password=None):
        setting = {}
        self.hadFirstStateUpdate = False
        self.hadFirstPlaylistIndex = False
        setting["name"] = roomName
        if password:
            setting["password"] = password
        self.sendSet({"room": setting})

    def sendFileSetting(self, file_):
        self.sendSet({"file": file_})
        self.sendList()

    def sendChatMessage(self, chatMessage):
        self.sendMessage({"Chat": chatMessage})

    def handleList(self, userList):
        self._client.userlist.clearList()
        for room in userList.items():
            roomName = room[0]
            for user in room[1].items():
                userName = user[0]
                file_ = user[1]['file'] if user[1]['file'] != {} else None
                isController = user[1]['controller'] if 'controller' in user[1] else False
                isReady = user[1]['isReady'] if 'isReady' in user[1] else None
                isAfk = user[1]['isAfk'] if 'isAfk' in user[1] else False
                features = user[1]['features'] if 'features' in user[1] else None
                self._client.userlist.addUser(userName, roomName, file_, noMessage=True, isController=isController, isReady=isReady, features=features, isAfk=isAfk)
        self._client.userlist.showUserList()

    def sendList(self):
        self.sendMessage({"List": None})

    def _extractStatePlaystateArguments(self, state):
        position = state["playstate"]["position"] if "position" in state["playstate"] else 0
        paused = state["playstate"]["paused"] if "paused" in state["playstate"] else None
        doSeek = state["playstate"]["doSeek"] if "doSeek" in state["playstate"] else None
        setBy = state["playstate"]["setBy"] if "setBy" in state["playstate"] else None
        return position, paused, doSeek, setBy

    def _handleStatePing(self, state):
        latencyCalculation = None  # a State whose ping block omits it must not blow up the handler
        if "latencyCalculation" in state["ping"]:
            latencyCalculation = state["ping"]["latencyCalculation"]
        if "clientLatencyCalculation" in state["ping"]:
            timestamp = state["ping"]["clientLatencyCalculation"]
            senderRtt = state["ping"]["serverRtt"]
            self._pingService.receiveMessage(timestamp, senderRtt)
        messageAge = self._pingService.getLastForwardDelay()
        return messageAge, latencyCalculation

    def handleState(self, state):
        position, paused, doSeek, setBy = None, None, None, None
        latencyCalculation = None  # sendState below reads it even when the State carried no ping block
        messageAge = 0
        if not self.hadFirstStateUpdate:
            self.hadFirstStateUpdate = True
        if "ignoringOnTheFly" in state:
            ignore = state["ignoringOnTheFly"]
            if "server" in ignore:
                self.serverIgnoringOnTheFly = ignore["server"]
                self.clientIgnoringOnTheFly = 0
            elif "client" in ignore:
                if(ignore['client']) == self.clientIgnoringOnTheFly:
                    self.clientIgnoringOnTheFly = 0
        if "playstate" in state:
            position, paused, doSeek, setBy = self._extractStatePlaystateArguments(state)
        if "ping" in state:
            messageAge, latencyCalculation = self._handleStatePing(state)
        if "yapTimer" in state:
            yap = state["yapTimer"]
            self._client.ui.updateYapTimer(
                yap.get("paused", False), yap.get("current", 0), yap.get("total", 0),
                yap.get("afkTotal", 0), yap.get("duration", None))
        if "pauseWarning" in state:
            self._client.ui.updatePauseWarning(state["pauseWarning"].get("message", ""))
        # Absence is meaningful here, unlike the two above: the server stops sending the block when
        # the hold ends, and that is how the client learns it may react to time differences again.
        # isinstance, not presence: a non-dict value here would otherwise latch the hold on for
        # ever, silently suppressing this client's own desync corrections while displaying nothing.
        hold = state["bufferHold"] if "bufferHold" in state else None
        hold = hold if isinstance(hold, dict) else None
        self._client.setBufferHoldActive(hold is not None)
        self._client.ui.updateBufferHold(hold)
        # A keypress we are still holding is newer than anything this message can be carrying: the
        # State that acknowledges our last change was composed by the server before it had heard
        # about this one. Applying it first would put the player back and then send *that* as the
        # user's action - the very thing the pending change exists to prevent.
        supersededByPendingChange = self._pendingStateChange and not self.clientIgnoringOnTheFly
        if position is not None and paused is not None and not self.clientIgnoringOnTheFly \
                and not supersededByPendingChange:
            self._client.updateGlobalState(position, paused, doSeek, setBy, messageAge)
        position, paused, doSeek, stateChange = self._client.getLocalState()
        if self._pendingStateChange and not self.clientIgnoringOnTheFly:
            # A keypress we could not send at the time (see sendState) - the acknowledgement has
            # arrived, so it goes out now, as the change it always was. Without this it is simply
            # lost, and the next State from the server puts the player back: on a link where a round
            # trip is most of a second, that is "I pressed pause and nothing happened".
            self._pendingStateChange = False
            stateChange = True
        self.sendState(position, paused, doSeek, latencyCalculation, stateChange)

    def sendState(self, position, paused, doSeek, latencyCalculation, stateChange=False):
        state = {}
        positionAndPausedIsSet = position is not None and paused is not None
        clientIgnoreIsNotSet = self.clientIgnoringOnTheFly == 0 or self.serverIgnoringOnTheFly != 0
        if clientIgnoreIsNotSet and positionAndPausedIsSet:
            state["playstate"] = {}
            state["playstate"]["position"] = position
            state["playstate"]["paused"] = paused
            if doSeek:
                state["playstate"]["doSeek"] = doSeek
        if self._client.isBuffering() or self._sentBuffering:
            # Reported for as long as it lasts, plus exactly one report of the falling edge so the
            # server can release its hold without waiting for the staleness timeout. Servers that
            # do not know the key ignore it (unknown State keys are silently dropped both ways).
            self._sentBuffering = self._client.isBuffering()
            state["buffering"] = {
                "active": self._client.isBuffering(),
                "cache": self._client.getBufferCachePercent(),
            }
        state["ping"] = {}
        if latencyCalculation:
            state["ping"]["latencyCalculation"] = latencyCalculation
        state["ping"]["clientLatencyCalculation"] = self._pingService.newTimestamp()
        state["ping"]["clientRtt"] = self._pingService.getRtt()
        if stateChange and not clientIgnoreIsNotSet:
            # The playstate was left out above because an earlier change is still unacknowledged, so
            # this one never reached the wire. Remember it instead of dropping it (handleState sends
            # it the moment the acknowledgement lands) and do not bump the counter for a message
            # nobody is going to see - that would only make the round trip longer.
            self._pendingStateChange = True
        elif stateChange:
            self.clientIgnoringOnTheFly += 1
        if self.serverIgnoringOnTheFly or self.clientIgnoringOnTheFly:
            state["ignoringOnTheFly"] = {}
            if self.serverIgnoringOnTheFly:
                state["ignoringOnTheFly"]["server"] = self.serverIgnoringOnTheFly
                self.serverIgnoringOnTheFly = 0
            if self.clientIgnoringOnTheFly:
                state["ignoringOnTheFly"]["client"] = self.clientIgnoringOnTheFly
        self.sendMessage({"State": state})

    def requestControlledRoom(self, room, password):
        self.sendSet({
            "controllerAuth": {
                "room": room,
                "password": password
            }
        })

    def handleChat(self, message):
        username = message['username']
        userMessage = message['message']
        self._client.ui.showChatMessage(username, userMessage)

    def setReady(self, isReady, manuallyInitiated=True, username=None):
        if username:
            self.sendSet({
                "ready": {
                    "isReady": isReady,
                    "manuallyInitiated": manuallyInitiated,
                    "username": username
                }
            })
        else:
            self.sendSet({
                "ready": {
                    "isReady": isReady,
                    "manuallyInitiated": manuallyInitiated
                }
            })

    def setAfk(self, isAfk, username=None):
        if username:
            self.sendSet({"afk": {"isAfk": isAfk, "username": username}})
        else:
            self.sendSet({"afk": {"isAfk": isAfk}})

    def setPlaylist(self, files):
        self.sendSet({
            "playlistChange": {
                "files": files
            }
        })

    def setPlaylistIndex(self, index):
        self.sendSet({
            "playlistIndex": {
                "index": index
            }
        })

    def handleError(self, error):
        if "startTLS" in error["message"] and not self.logged:
            self._client._serverSupportsTLS = False
        else:
            self.dropWithError(error["message"])

    def sendError(self, message):
        self.sendMessage({"Error": {"message": message}})

    def sendTLS(self, message):
        self.sendMessage({"TLS": message})

    def handleTLS(self, message):
        answer = message["startTLS"] if "startTLS" in message else None
        if "true" in answer and not self.logged and self._client.protocolFactory.options is not None:
            self.transport.startTLS(self._client.protocolFactory.options)
            # To be deleted when the support for Twisted between >=16.4.0 and < 17.1.0 is dropped
            minTwistedVersion = Version('twisted', 17, 1, 0)
            if twistedVersion < minTwistedVersion:
                self._client.protocolFactory.options._ctx.set_info_callback(self.customHandshakeCallback)
        elif "false" in answer:
            self._client.ui.showErrorMessage(getMessage("startTLS-not-supported-server"))
            self.sendHello()

    def customHandshakeCallback(self, conn, where, ret):
        # To be deleted when the support for Twisted between >=16.4.0 and < 17.1.0 is dropped
        from OpenSSL.SSL import SSL_CB_HANDSHAKE_START, SSL_CB_HANDSHAKE_DONE
        if where == SSL_CB_HANDSHAKE_START:
            self._client.ui.showDebugMessage("TLS handshake started")
        if where == SSL_CB_HANDSHAKE_DONE:
            self._client.ui.showDebugMessage("TLS handshake done")
            self.handshakeCompleted()

    def handshakeCompleted(self):
        self._serverCertificateTLS = self.transport.getPeerCertificate()
        if not self._serverCertificateTLS:
            self._client.ui.showErrorMessage(getMessage("startTLS-server-certificate-invalid"))
            self.sendHello()
            return

        self._subjectTLS = ""
        try:
            from cryptography import x509
            cryptographyCertificate = self._serverCertificateTLS.to_cryptography()
            subjectAltName = cryptographyCertificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            names = subjectAltName.get_values_for_type(x509.DNSName)
            names = names + ["IP Address:{}".format(ipAddress) for ipAddress in subjectAltName.get_values_for_type(x509.IPAddress)]
            self._subjectTLS = ", ".join([str(name) for name in names])
        except Exception:
            pass

        if not self._subjectTLS:
            try:
                if hasattr(self._serverCertificateTLS, "get_extension_count") and hasattr(self._serverCertificateTLS, "get_extension"):
                    for x in range(0,self._serverCertificateTLS.get_extension_count()):
                        extension = self._serverCertificateTLS.get_extension(x)
                        if extension.get_short_name() == b'subjectAltName':
                            self._subjectTLS = str(extension).replace("DNS:", "")
                            break
            except Exception:
                pass

        if not self._subjectTLS:
            self._subjectTLS = self._client._config.get("host", "") or ""

        self._issuerTLS = self._serverCertificateTLS.get_issuer().CN
        self._expiredTLS =self._serverCertificateTLS.has_expired()
        self._expireDateTLS = datetime.strptime(self._serverCertificateTLS.get_notAfter().decode('ascii'), '%Y%m%d%H%M%SZ')

        self._encryptedConnectionTLS = self.transport.protocol._tlsConnection
        self._connVersionNumberTLS = self._encryptedConnectionTLS.get_protocol_version()
        self._connVersionStringTLS = self._encryptedConnectionTLS.get_protocol_version_name()
        self._cipherNameTLS = self._encryptedConnectionTLS.get_cipher_name()
    
        if self._connVersionNumberTLS == 771:
            self._connVersionNumberTLS = '1.2'
        elif self._connVersionNumberTLS == 772:
            self._connVersionNumberTLS = '1.3'

        self._client.ui.showMessage(getMessage("startTLS-secure-connection-ok").format(self._connVersionStringTLS))
        self._client.ui.setSSLMode( True,
                                    {'subject': self._subjectTLS, 'issuer': self._issuerTLS, 'expires': self._expireDateTLS,
                                    'protocolString': self._connVersionStringTLS, 'protocolVersion': self._connVersionNumberTLS,
                                    'cipher': self._cipherNameTLS})

        self.sendHello()


class SyncServerProtocol(JSONCommandProtocol):
    def __init__(self, factory):
        self._factory = factory
        self._version = None
        self._features = None
        self._logged = False
        self.clientIgnoringOnTheFly = 0
        self.serverIgnoringOnTheFly = 0
        self._pingService = PingService()
        self._clientLatencyCalculation = 0
        self._clientLatencyCalculationArrivalTime = 0
        self._watcher = None

    def __hash__(self):
        return hash('|'.join((
            self.transport.getPeer().host,
            str(id(self)),
        )))

    def requireLogged(f):  # @NoSelf
        @wraps(f)
        def wrapper(self, *args, **kwds):
            if not self._logged:
                self.dropWithError(getMessage("not-known-server-error"))
            return f(self, *args, **kwds)
        return wrapper

    def showDebugMessage(self, line):
        pass

    def dropWithError(self, error):
        print(getMessage("client-drop-server-error").format(self.transport.getPeer().host, error))
        self.sendError(error)
        self.drop()

    def connectionLost(self, reason):
        self._factory.removeWatcher(self._watcher)

    def getFeatures(self):
        if not self._features:
            self._features = {}
            self._features["sharedPlaylists"] = meetsMinVersion(self._version, SHARED_PLAYLIST_MIN_VERSION)
            self._features["chat"] = meetsMinVersion(self._version, CHAT_MIN_VERSION)
            self._features["featureList"] = False
            self._features["readiness"] = meetsMinVersion(self._version, USER_READY_MIN_VERSION)
            self._features["managedRooms"] = meetsMinVersion(self._version, CONTROLLED_ROOMS_MIN_VERSION)
            self._features["persistentRooms"] = False
            self._features["uiMode"] = UNKNOWN_UI_MODE
        return self._features

    def isLogged(self):
        return self._logged

    def hasOutstandingForcedUpdate(self):
        # A forced state update is still waiting to be echoed back, so this client's own reports
        # are being ignored: sending another one would only push the counters further apart.
        return self.serverIgnoringOnTheFly != 0

    def meetsMinVersion(self, version):
        return self._version >= version

    def getVersion(self):
        return self._version

    def _extractHelloArguments(self, hello):
        roomName = None
        if "username" in hello:
            username = hello["username"]
            username = username.strip()
        else:
            username = None
        serverPassword = hello["password"] if "password" in hello else None
        room = hello["room"] if "room" in hello else None
        if room:
            if "name" in room:
                roomName = room["name"]
                roomName = roomName.strip()
            else:
                roomName = None
        version = hello["version"] if "version" in hello else None
        version = hello["realversion"] if "realversion" in hello else version
        features = hello["features"] if "features" in hello else None
        return username, serverPassword, roomName, version, features

    def _checkPassword(self, serverPassword):
        if self._factory.password:
            if not serverPassword:
                self.dropWithError(getMessage("password-required-server-error"))
                return False
            if serverPassword != self._factory.password:
                self.dropWithError(getMessage("wrong-password-server-error"))
                return False
        return True

    def handleHello(self, hello):
        username, serverPassword, roomName, version, features = self._extractHelloArguments(hello)
        if not username or not roomName or not version:
            self.dropWithError(getMessage("hello-server-error"))
            return
        else:
            if not self._checkPassword(serverPassword):
                return
            self._version = version
            self.setFeatures(features)
            self._factory.addWatcher(self, username, roomName)
            self._logged = True
            self.sendHello(version)
            # Only now is it safe to hand over the room's sticky state: the client wipes its
            # session-only copy of it while handling Hello, so a Set sent during addWatcher (which
            # runs before this point) would arrive first and be thrown away.
            self._factory.sendRoomStateToWatcher(self._watcher)
            # Watcher.setRoom already tried to seek this client to the room position, but that
            # attempt was dropped: Watcher.sendState is gated on isLogged() and addWatcher runs
            # before _logged is set. Retry it now for anyone whose player was already open on the
            # file - the file has not been announced yet, hence requireFile=False.
            self._factory.pullWatcherIntoSyncIfNeeded(self._watcher, requireFile=False)

    def persistentRoomWarning(self, clientFeatures, serverFeatures):
        serverPersistentRooms = serverFeatures["persistentRooms"]
        clientPersistentRooms = clientFeatures["persistentRooms"] if "persistentRooms" in clientFeatures else False
        return serverPersistentRooms and not clientPersistentRooms

    @requireLogged
    def handleChat(self, chatMessage):
        if not self._factory.disableChat:
            self._factory.sendChat(self._watcher, chatMessage)

    def setFeatures(self, features):
        self._features = features

    def sendFeaturesUpdate(self):
        self.sendSet({"features": self.getFeatures()})

    def setWatcher(self, watcher):
        self._watcher = watcher

    def sendHello(self, clientVersion):
        hello = {}
        username = self._watcher.getName()
        hello["username"] = username
        userIp = self.transport.getPeer().host
        room = self._watcher.getRoom()
        if room:
            hello["room"] = {"name": room.getName()}
        hello["version"] = clientVersion  # Used so 1.2.X client works on newer server
        hello["realversion"] = syncplay.version
        hello["features"] = self._factory.getFeatures()
        hello["motd"] = self._factory.getMotd(userIp, username, room, clientVersion)
        if self.persistentRoomWarning(clientFeatures=self._features, serverFeatures=hello["features"]):
            span = "\n\n" if hello["motd"] else ""
            hello["motd"] = getMessage("persistent-rooms-notice") + span + hello["motd"]
        self.sendMessage({"Hello": hello})

    @requireLogged
    def handleSet(self, settings):
        for set_ in settings.items():
            command = set_[0]
            if command == "room":
                roomName = set_[1]["name"] if "name" in set_[1] else None
                self._factory.setWatcherRoom(self._watcher, roomName)
            elif command == "file":
                self._watcher.setFile(set_[1])
            elif command == "controllerAuth":
                password = set_[1]["password"] if "password" in set_[1] else None
                room = set_[1]["room"] if "room" in set_[1] else None
                self._factory.authRoomController(self._watcher, password, room)
            elif command == "ready":
                manuallyInitiated = set_[1]['manuallyInitiated'] if "manuallyInitiated" in set_[1] else False
                username = set_[1]['username'] if "username" in set_[1] else None
                self._factory.setReady(self._watcher, set_[1]['isReady'], manuallyInitiated=manuallyInitiated, username=username)
            elif command == "playlistChange":
                self._factory.setPlaylist(self._watcher, set_[1]['files'])
            elif command == "playlistIndex":
                self._factory.setPlaylistIndex(self._watcher, set_[1]['index'])
            elif command == "features":
                # TODO: Check
                self._watcher.setFeatures(set_[1])
            elif command == "adminAuth":
                password = set_[1].get("password") if isinstance(set_[1], dict) else None
                self._factory.authAdmin(self._watcher, password)
            elif command == "trackProposal":
                self._factory.setTrackProposal(self._watcher, set_[1])
            elif command == "trustedDomains":
                self._factory.setTrustedDomains(self._watcher, set_[1])
            elif command == "afk":
                isAfk = set_[1].get("isAfk") if isinstance(set_[1], dict) else None
                username = set_[1].get("username") if isinstance(set_[1], dict) else None
                self._factory.setAfk(self._watcher, bool(isAfk), username=username)

    def sendSet(self, setting):
        self.sendMessage({"Set": setting})

    def sendNewControlledRoom(self, roomName, password):
        self.sendSet({
            "newControlledRoom": {
                "password": password,
                "roomName": roomName
            }
        })

    def sendControlledRoomAuthStatus(self, success, username, roomname):
        self.sendSet({
            "controllerAuth": {
                "user": username,
                "room": roomname,
                "success": success
            }
        })

    def sendSetReady(self, username, isReady, manuallyInitiated=True, setByUsername=None):
        if setByUsername:
            self.sendSet({
                "ready": {
                    "username": username,
                    "isReady": isReady,
                    "manuallyInitiated": manuallyInitiated,
                    "setBy": setByUsername
                }
            })
        else:
            self.sendSet({
                "ready": {
                    "username": username,
                    "isReady": isReady,
                    "manuallyInitiated": manuallyInitiated
                }
            })

    def sendSetAfk(self, username, isAfk, setBy=None):
        if setBy:
            self.sendSet({"afk": {"username": username, "isAfk": isAfk, "setBy": setBy}})
        else:
            self.sendSet({"afk": {"username": username, "isAfk": isAfk}})

    def sendRoomLock(self, roomName, locked, setBy=None):
        if setBy:
            self.sendSet({"roomLock": {"room": roomName, "locked": locked, "setBy": setBy}})
        else:
            self.sendSet({"roomLock": {"room": roomName, "locked": locked}})

    def setPlaylist(self, username, files):
        self.sendSet({
            "playlistChange": {
                "user": username,
                "files": files
            }
        })

    def setPlaylistIndex(self, username, index):
        self.sendSet({
            "playlistIndex": {
                "user": username,
                "index": index
            }
        })

    def sendUserSetting(self, username, room, file_, event):
        room = {"name": room.getName()}
        user = {username: {}}
        user[username]["room"] = room
        if file_:
            user[username]["file"] = file_
        if event:
            user[username]["event"] = event
        self.sendSet({"user": user})

    def _addUserOnList(self, userlist, watcher):
        room = watcher.getRoom()
        if room:
            if room.getName() not in userlist:
                userlist[room.getName()] = {}
            userFile = {
                "position": 0,
                "file": watcher.getFile() if watcher.getFile() else {},
                "controller": watcher.isController(),
                "isReady": watcher.isReady(),
                "isAfk": watcher.isAfk(),
                "features": watcher.getFeatures()
            }
            userlist[room.getName()][watcher.getName()] = userFile

    def _addDummyUserOnList(self, userlist, dummyRoom,dummyCount):
        if dummyRoom not in userlist:
            userlist[dummyRoom] = {}
        dummyFile = {
            "position": 0,
            "file": {},
            "controller": False,
            "isReady": True,
            "features": []
        }
        userlist[dummyRoom][" " * dummyCount] = dummyFile

    def sendList(self):
        userlist = {}
        watchers = self._factory.getAllWatchersForUser(self._watcher)
        dummyCount = 0
        for watcher in watchers:
            self._addUserOnList(userlist, watcher)
        if self._watcher.isGUIUser(self.getFeatures()):
            for emptyRoom in self._factory.getEmptyPersistentRooms():
                dummyCount += 1
                self._addDummyUserOnList(userlist, emptyRoom, dummyCount)
        self.sendMessage({"List": userlist})

    @requireLogged
    def handleList(self, _):
        self.sendList()

    def sendState(self, position, paused, doSeek, setBy, forced=False):
        if self._clientLatencyCalculationArrivalTime:
            processingTime = time.time() - self._clientLatencyCalculationArrivalTime
        else:
            processingTime = 0
        playstate = {
                     "position": position if position else 0,
                     "paused": paused,
                     "doSeek": doSeek,
                     "setBy": setBy.getName() if setBy else None
        }
        ping = {
                "latencyCalculation": self._pingService.newTimestamp(),
                "serverRtt": self._pingService.getRtt()
                }
        if self._clientLatencyCalculation:
            ping["clientLatencyCalculation"] = self._clientLatencyCalculation + processingTime
            self._clientLatencyCalculation = 0
        state = {
                 "ping": ping,
                 "playstate": playstate,
                }
        room = self._watcher.getRoom() if self._watcher else None
        # This 1s tick doubles as the lazy check that trips the give-up state once a single
        # pause exceeds YAP_TIMER_MAX_PAUSE; while expired, neither field is sent.
        yapExpired = room.yapCheckExpired() if room else False
        if self._factory.yapTimer and room and not yapExpired and self._watcher.supportsFeature("yapTimer"):
            state["yapTimer"] = {
                "paused": room.isPaused(),
                "current": room.yapCurrentElapsed(),
                "total": room.yapTotal(),
                "afkTotal": room.yapAfkTotal(),  # of the total, time spent with an AFK watcher present
                "duration": self._factory._getRoomFileDuration(room),  # current file runtime, for the drag ratio (None if unknown)
            }
        if self._factory.pauseWarningAfter and room and room._pauseWarningActive \
                and not yapExpired and not room.hasAfkWatcher() \
                and self._watcher.supportsFeature("pauseWarning"):
            state["pauseWarning"] = {"message": self._factory.pauseWarningText(room)}
        if room and room.bufferHoldIsActive() and self._watcher.supportsFeature("bufferPause"):
            state["bufferHold"] = {
                "user": room.bufferHoldBy(),
                "elapsed": room.bufferHoldElapsed(),
                "cache": room.bufferHoldCachePercent(),
            }
        if forced:
            self.serverIgnoringOnTheFly += 1
        if self.serverIgnoringOnTheFly or self.clientIgnoringOnTheFly:
            state["ignoringOnTheFly"] = {}
            if self.serverIgnoringOnTheFly:
                state["ignoringOnTheFly"]["server"] = self.serverIgnoringOnTheFly
            if self.clientIgnoringOnTheFly:
                state["ignoringOnTheFly"]["client"] = self.clientIgnoringOnTheFly
                self.clientIgnoringOnTheFly = 0
        if self.serverIgnoringOnTheFly == 0 or forced:
            self.sendMessage({"State": state})

    def _extractStatePlaystateArguments(self, state):
        position = state["playstate"]["position"] if "position" in state["playstate"] else 0
        paused = state["playstate"]["paused"] if "paused" in state["playstate"] else None
        doSeek = state["playstate"]["doSeek"] if "doSeek" in state["playstate"] else None
        return position, paused, doSeek

    @requireLogged
    def handleState(self, state):
        position, paused, doSeek, latencyCalculation = None, None, None, None
        clientFlaggedChange = False
        if "ignoringOnTheFly" in state:
            ignore = state["ignoringOnTheFly"]
            if "server" in ignore:
                if self.serverIgnoringOnTheFly == ignore["server"]:
                    self.serverIgnoringOnTheFly = 0
            if "client" in ignore:
                # The client bumps this counter in the same message that carries a change it made,
                # and omits the playstate entirely while an earlier one is unacknowledged - so this
                # key is how a deliberate keypress announces itself. Its absence is what lets the
                # stale-echo guard tell a heartbeat from a command (Watcher._pauseReportIsStaleEcho).
                clientFlaggedChange = True
                self.clientIgnoringOnTheFly = ignore["client"]
        if "playstate" in state:
            position, paused, doSeek = self._extractStatePlaystateArguments(state)
        if "ping" in state:
            latencyCalculation = state["ping"]["latencyCalculation"] if "latencyCalculation" in state["ping"] else 0
            clientRtt = state["ping"]["clientRtt"] if "clientRtt" in state["ping"] else 0
            self._clientLatencyCalculation = state["ping"]["clientLatencyCalculation"] if "clientLatencyCalculation" in state["ping"] else 0
            self._clientLatencyCalculationArrivalTime = time.time()
            self._pingService.receiveMessage(latencyCalculation, clientRtt)
        if "buffering" in state:
            # Deliberately outside the ignoring-on-the-fly gate below. Forcing the hold's pause on
            # this client makes it ignore on the fly, and while it does it sends playstate-less
            # States - so gating this too would drop the very report that ends the hold, leaving it
            # to expire on the staleness timeout instead.
            # isinstance, not truthiness: a peer is free to send anything at all under this key,
            # and `null`/a string/a list would otherwise raise straight out of lineReceived and
            # cost that client its connection (plus a traceback in the server log) for one bad
            # line. Everything else off the wire in this feature is re-validated; so is this.
            buffering = state["buffering"]
            if isinstance(buffering, dict):
                self._watcher.updateBuffering(buffering.get("active", False), buffering.get("cache"))
        if self.serverIgnoringOnTheFly == 0:
            echoCandidate = "playstate" in state and not clientFlaggedChange
            self._watcher.updateState(position, paused, doSeek,
                                      self._pingService.getLastForwardDelay(), echoCandidate)

    def handleError(self, error):
        self.dropWithError(error["message"])  # TODO: more processing and fallbacking

    def sendError(self, message):
        self.sendMessage({"Error": {"message": message}})

    def sendTLS(self, message):
        self.sendMessage({"TLS": message})

    def handleTLS(self, message):
        inquiry = message["startTLS"] if "startTLS" in message else None
        if "send" in inquiry:
            if not self.isLogged() and self._factory.serverAcceptsTLS:
                lastEditCertTime = self._factory.checkLastEditCertTime()
                if lastEditCertTime is not None and lastEditCertTime != self._factory.lastEditCertTime:
                    self._factory.updateTLSContextFactory()
                if self._factory.options is not None:
                    self.sendTLS({"startTLS": "true"})
                    self.transport.startTLS(self._factory.options)
                else:
                    self.sendTLS({"startTLS": "false"})
            else:
                self.sendTLS({"startTLS": "false"})


class PingService(object):
    """Estimates the one-way (forward) delay of incoming State messages.

    The estimate is consumed as `position += messageAge`, so it is only ever allowed to be
    approximately right - a wrong estimate does not merely fail to compensate, it actively moves
    the listener's idea of where the room is. Everything here is therefore biased towards
    under-compensating rather than trusting a noisy sample.
    """

    def __init__(self):
        self._rtt = 0
        self._fd = 0
        self._avrRtt = 0
        self._avrAsymmetry = 0

    def newTimestamp(self):
        return time.time()

    def receiveMessage(self, timestamp, senderRtt):
        if not timestamp:
            return
        rtt = time.time() - timestamp
        if rtt < 0 or senderRtt < 0:
            return
        self._rtt = rtt
        if not self._avrRtt:
            self._avrRtt = rtt

        # A round trip far above the running average is a congestion/retransmit spike. It is real,
        # but it describes one packet rather than the path, so it is kept out of the estimate
        # instead of being allowed to define it.
        isOutlier = rtt > self._avrRtt * constants.PING_OUTLIER_FACTOR + constants.PING_OUTLIER_MARGIN
        weight = constants.PING_OUTLIER_AVERAGE_WEIGHT if isOutlier else PING_MOVING_AVERAGE_WEIGHT
        self._avrRtt = self._avrRtt * weight + rtt * (1 - weight)

        # Asymmetry correction: if our round trip consistently exceeds the peer's, the extra delay
        # plausibly sits in the forward direction and belongs in the estimate. "Consistently" is
        # the operative word - senderRtt was measured by the peer at a different moment, so on a
        # jittery link a single difference between the two samples is noise. It is clamped, then
        # smoothed, and it decays back to zero as soon as the evidence stops.
        asymmetry = 0
        if senderRtt > 0 and not isOutlier and senderRtt < rtt:
            asymmetry = min(rtt - senderRtt, constants.PING_MAX_ASYMMETRY_CORRECTION)
        self._avrAsymmetry = self._avrAsymmetry * PING_MOVING_AVERAGE_WEIGHT \
            + asymmetry * (1 - PING_MOVING_AVERAGE_WEIGHT)

        forwardDelay = self._avrRtt / 2 + self._avrAsymmetry
        self._fd = max(0.0, min(forwardDelay, constants.PING_MAX_FORWARD_DELAY))

    def getLastForwardDelay(self):
        return self._fd

    def getRtt(self):
        return self._rtt
