from syncplay import constants


class BasePlayer(object):

    # Players that can render the live yap-timer overlay set this True and override updateYapTimerOSD.
    yapTimerOSDSupported = False
    # Players that can render the blinking pause-warning overlay set this True and override updatePauseWarningOSD.
    pauseWarningOSDSupported = False
    # Players that can render generic styled/ASS OSD messages set this True and override showGenericOSD.
    genericOSDSupported = False
    # Players that can apply/publish admin track proposals set this True and override the two methods.
    trackProposalsSupported = False
    # Players that can render the live buffer-hold overlay set this True and override updateBufferHoldOSD.
    bufferHoldOSDSupported = False
    # Players that can say for themselves whether they are stalled filling a cache set this True and
    # override getBufferState. Everything else falls back to the client's position-stall heuristic.
    bufferStateSupported = False

    '''
    This method is supposed to
    execute updatePlayerStatus(paused, position) on client
    Given the arguments: boolean paused and float position in seconds
    '''
    def askForStatus(self):
        raise NotImplementedError()

    '''
    Show/refresh the persistent yap-timer overlay with the given text (empty string hides it).
    No-op for players that do not support it.
    '''
    def updateYapTimerOSD(self, text):
        pass

    '''
    Show/refresh the blinking pause-warning overlay with the given text (empty string hides it).
    No-op for players that do not support it.
    '''
    def updatePauseWarningOSD(self, text):
        pass

    '''
    Show/refresh the live buffer-hold overlay with the given text (empty string hides it).
    No-op for players that do not support it.
    '''
    def updateBufferHoldOSD(self, text):
        pass

    '''
    Whether the player is currently stalled filling its cache, as (stalled, cachePercent).
    cachePercent is None when the player does not report one. Returns None - not (False, None) -
    when the player cannot answer at all, which is what sends the client to its stall heuristic
    instead. No-op players inherit that None.
    '''
    def getBufferState(self):
        return None

    '''
    Display a generic server-driven OSD message. isAss=True means text contains raw ASS override
    tags to be rendered as-is; assAlignment is an ASS \\an value (1-9); colour is "#RRGGBB";
    size is an ASS \\fs value; duration is in seconds. No-op for players that do not support it.
    '''
    def showGenericOSD(self, text, isAss, assAlignment, colour, size, duration):
        pass

    '''
    Hand an admin track proposal (audio/sub ids + layout signature) to the player, which applies
    it when the local track layout matches. No-op for players that do not support it.
    '''
    def setTrackProposal(self, payload):
        pass

    '''
    Ask the player to read its current track selection and publish it as a proposal (the result
    comes back asynchronously via client.publishTrackProposal). No-op if unsupported.
    '''
    def requestTrackPublish(self):
        pass

    '''
    Display given message on player's OSD or similar means
    '''
    def displayMessage(
        self, message, duration=(constants.OSD_DURATION*1000), OSDType=constants.OSD_NOTIFICATION, mood=constants.MESSAGE_NEUTRAL
    ):
        raise NotImplementedError()

    '''
    Cleanup connection with player before syncplay will close down
    '''
    def drop(self):
        raise NotImplementedError()

    '''
    Start up the player, returns its instance
    '''
    @staticmethod
    def run(client, playerPath, filePath, args):
        raise NotImplementedError()

    '''
    @type value: boolean
    '''
    def setPaused(self, value):
        raise NotImplementedError()

    '''
        @type value: list
        '''
    def setFeatures(self, featureList):
        raise NotImplementedError()

    '''
    @type value: float
    '''
    def setPosition(self, value):
        raise NotImplementedError()

    '''
    @type value: float
    '''
    def setSpeed(self, value):
        raise NotImplementedError()

    '''
    @type filePath: string
    '''
    def openFile(self, filePath, resetPosition=False):
        raise NotImplementedError()

    '''
    @return: list of strings
    '''
    @staticmethod
    def getDefaultPlayerPathsList():
        raise NotImplementedError()

    '''
    @type path: string
    '''
    @staticmethod
    def isValidPlayerPath(path):
        raise NotImplementedError()

    '''
    @type path: string
    @return: string
    '''
    @staticmethod
    def getIconPath(path):
        raise NotImplementedError()

    '''
    @type path: string
    @return: string
    '''
    @staticmethod
    def getExpandedPath(path):
        raise NotImplementedError()

    '''
    Opens a custom media browse dialog, and then changes to that media if appropriate
    '''
    @staticmethod
    def openCustomOpenDialog(self):
        raise NotImplementedError()

    '''
    @type playerPath: string
    @type filePath: string
    @return errorMessage: string

    Checks if the player has any problems with the given player/file path
    '''
    @staticmethod
    def getPlayerPathErrors(playerPath, filePath):
        raise NotImplementedError()


class DummyPlayer(BasePlayer):

    @staticmethod
    def getDefaultPlayerPathsList():
        return []

    @staticmethod
    def isValidPlayerPath(path):
        return False

    @staticmethod
    def getIconPath(path):
        return None

    @staticmethod
    def getExpandedPath(path):
        return path

    @staticmethod
    def getPlayerPathErrors(playerPath, filePath):
        return None
