import inspect

from telethon.tl import types
from telethon.tl.functions import account, bots, channels, fragment, messages, payments, premium
from telethon.tl.functions import help as helpfn
from telethon.tl.functions import stats as statsfn

MODS = {
    "channels": channels,
    "messages": messages,
    "account": account,
    "payments": payments,
    "premium": premium,
    "stats": statsfn,
    "help": helpfn,
    "bots": bots,
    "fragment": fragment,
}

ITEMS = """
channels:EditLocationRequest channels:UpdateColorRequest channels:UpdateEmojiStatusRequest
channels:SetMainProfileTabRequest channels:ToggleSlowModeRequest channels:SetStickersRequest
channels:SetEmojiStickersRequest channels:RestrictSponsoredMessagesRequest
channels:ToggleAutotranslationRequest channels:ToggleParticipantsHiddenRequest
channels:ToggleAntiSpamRequest channels:ToggleJoinToSendRequest channels:ToggleJoinRequestRequest
channels:TogglePreHistoryHiddenRequest channels:ToggleViewForumAsMessagesRequest
channels:CheckUsernameRequest channels:ExportMessageLinkRequest channels:ReportSpamRequest
channels:DeleteParticipantHistoryRequest channels:GetChannelRecommendationsRequest
channels:GetSendAsRequest channels:ToggleUsernameRequest channels:ReorderUsernamesRequest
messages:MigrateChatRequest messages:EditChatCreatorRequest messages:EditChatParticipantRankRequest
messages:GetAdminsWithInvitesRequest messages:CheckChatInviteRequest messages:ImportChatInviteRequest
messages:HideChatJoinRequestRequest messages:HideAllChatJoinRequestsRequest
messages:ReadDiscussionRequest messages:DeleteTopicHistoryRequest
messages:ReorderPinnedForumTopicsRequest messages:UpdatePinnedForumTopicRequest
messages:GetForumTopicsByIDRequest messages:ReportSponsoredMessageRequest
messages:ToggleSuggestedPostApprovalRequest messages:GetSponsoredMessagesRequest
messages:GetExportedChatInviteRequest messages:DeleteExportedChatInviteRequest
messages:DeleteRevokedExportedChatInvitesRequest messages:GetExportedChatInvitesRequest
messages:SaveDefaultSendAsRequest messages:ToggleNoForwardsRequest
messages:EditChatDefaultBannedRightsRequest messages:EditChatAdminRequest
account:ToggleNoPaidMessagesExceptionRequest account:GetPaidMessagesRevenueRequest
payments:GetStarsRevenueStatsRequest payments:ToggleChatStarGiftNotificationsRequest
payments:ConnectStarRefBotRequest payments:EditConnectedStarRefBotRequest
payments:GetConnectedStarRefBotsRequest payments:GetSuggestedStarRefBotsRequest
premium:ApplyBoostRequest premium:GetUserBoostsRequest premium:GetBoostsStatusRequest
stats:LoadAsyncGraphRequest stats:GetMessagePublicForwardsRequest
stats:GetStoryPublicForwardsRequest stats:GetMessageStatsRequest stats:GetStoryStatsRequest
stats:GetPollStatsRequest stats:GetBroadcastStatsRequest stats:GetMegagroupStatsRequest
help:DismissSuggestionRequest bots:SetCustomVerificationRequest
fragment:GetCollectibleInfoRequest
""".split()

for item in ITEMS:
    mod, name = item.split(":")
    print(item, inspect.signature(getattr(MODS[mod], name).__init__))

print()
for name in (
    "ForumTopic",
    "ChannelParticipant",
    "ChannelParticipantSelf",
    "ChannelParticipantCreator",
    "ChannelParticipantAdmin",
    "ChannelParticipantBanned",
    "ChannelParticipantLeft",
    "ChatInviteExported",
    "ChatInviteImporter",
    "ChatAdminWithInvites",
    "ChannelAdminLogEvent",
    "ChannelAdminLogEventsFilter",
    "Boost",
    "MyBoost",
    "ChatParticipantAdmin",
    "ChatParticipantCreator",
    "ChatParticipant",
    "StatsGraphAsync",
    "StatsGraph",
    "ChatInvite",
    "ChatInvitePeek",
    "ChatInviteAlready",
    "StarsSubscriptionPricing",
    "InputChatUploadedPhoto",
    "SponsoredMessage",
):
    print(name, inspect.signature(getattr(types, name).__init__))
