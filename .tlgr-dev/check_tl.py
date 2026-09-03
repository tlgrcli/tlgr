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

WANT = """
channels:EditAdminRequest channels:EditBannedRequest channels:GetParticipantsRequest
channels:GetParticipantRequest channels:InviteToChannelRequest channels:DeleteParticipantHistoryRequest
channels:ReportSpamRequest channels:GetAdminLogRequest channels:ReportAntiSpamFalsePositiveRequest
channels:CreateChannelRequest channels:EditTitleRequest channels:EditPhotoRequest
channels:EditLocationRequest channels:UpdateColorRequest channels:UpdateEmojiStatusRequest
channels:SetMainProfileTabRequest channels:ConvertToGigagroupRequest channels:GetGroupsForDiscussionRequest
channels:SetDiscussionGroupRequest channels:TogglePreHistoryHiddenRequest channels:ToggleSlowModeRequest
channels:ToggleJoinToSendRequest channels:ToggleJoinRequestRequest channels:ToggleParticipantsHiddenRequest
channels:ToggleAntiSpamRequest channels:ToggleSignaturesRequest channels:ToggleForumRequest
channels:ToggleViewForumAsMessagesRequest channels:ToggleAutotranslationRequest
channels:RestrictSponsoredMessagesRequest channels:SetStickersRequest channels:SetEmojiStickersRequest
channels:UpdatePaidMessagesPriceRequest channels:UpdateUsernameRequest channels:ReorderUsernamesRequest
channels:ToggleUsernameRequest channels:DeactivateAllUsernamesRequest channels:CheckUsernameRequest
channels:JoinChannelRequest channels:GetSendAsRequest channels:GetChannelRecommendationsRequest
channels:ExportMessageLinkRequest channels:GetFullChannelRequest channels:SetBoostsToUnblockRestrictionsRequest
messages:CreateChatRequest messages:AddChatUserRequest messages:DeleteChatUserRequest
messages:EditChatAdminRequest messages:EditChatDefaultBannedRightsRequest messages:EditChatAboutRequest
messages:EditChatTitleRequest messages:EditChatPhotoRequest messages:MigrateChatRequest
messages:EditChatCreatorRequest messages:EditChatParticipantRankRequest messages:ExportChatInviteRequest
messages:EditExportedChatInviteRequest messages:DeleteExportedChatInviteRequest
messages:DeleteRevokedExportedChatInvitesRequest messages:GetExportedChatInvitesRequest
messages:GetExportedChatInviteRequest messages:GetAdminsWithInvitesRequest messages:GetChatInviteImportersRequest
messages:CheckChatInviteRequest messages:ImportChatInviteRequest messages:HideChatJoinRequestRequest
messages:HideAllChatJoinRequestsRequest messages:CreateForumTopicRequest messages:EditForumTopicRequest
messages:GetForumTopicsRequest messages:GetForumTopicsByIDRequest messages:UpdatePinnedForumTopicRequest
messages:ReorderPinnedForumTopicsRequest messages:DeleteTopicHistoryRequest messages:ReadDiscussionRequest
messages:GetSavedDialogsRequest messages:SaveDefaultSendAsRequest messages:ToggleNoForwardsRequest
messages:SetChatAvailableReactionsRequest messages:GetSponsoredMessagesRequest
messages:ReportSponsoredMessageRequest messages:ToggleSuggestedPostApprovalRequest
messages:GetFullChatRequest messages:ReadMentionsRequest messages:ReadReactionsRequest
account:UpdateNotifySettingsRequest account:ToggleNoPaidMessagesExceptionRequest
account:GetPaidMessagesRevenueRequest account:GetPasswordRequest
payments:GetStarsRevenueStatsRequest payments:GetStarsTransactionsRequest
payments:ConnectStarRefBotRequest payments:EditConnectedStarRefBotRequest
payments:GetConnectedStarRefBotsRequest payments:GetSuggestedStarRefBotsRequest
payments:ToggleChatStarGiftNotificationsRequest
premium:ApplyBoostRequest premium:GetMyBoostsRequest premium:GetBoostsStatusRequest
premium:GetBoostsListRequest premium:GetUserBoostsRequest
stats:GetBroadcastStatsRequest stats:GetMegagroupStatsRequest stats:GetMessageStatsRequest
stats:GetStoryStatsRequest stats:GetPollStatsRequest stats:LoadAsyncGraphRequest
stats:GetMessagePublicForwardsRequest stats:GetStoryPublicForwardsRequest
help:DismissSuggestionRequest help:GetAppConfigRequest help:GetPeerColorsRequest
help:GetPeerProfileColorsRequest bots:SetCustomVerificationRequest fragment:GetCollectibleInfoRequest
""".split()

missing = []
for item in WANT:
    mod, name = item.split(":")
    obj = getattr(MODS[mod], name, None)
    if obj is None:
        missing.append(item)
print("MISSING:", missing)

for item in [
    "channels:EditAdminRequest",
    "channels:EditBannedRequest",
    "channels:GetAdminLogRequest",
    "channels:CreateChannelRequest",
    "messages:ExportChatInviteRequest",
    "messages:EditExportedChatInviteRequest",
    "messages:GetChatInviteImportersRequest",
    "messages:CreateForumTopicRequest",
    "messages:EditForumTopicRequest",
    "messages:GetForumTopicsRequest",
    "channels:ToggleForumRequest",
    "channels:ToggleSignaturesRequest",
    "messages:SetChatAvailableReactionsRequest",
    "channels:UpdatePaidMessagesPriceRequest",
    "premium:GetBoostsListRequest",
    "payments:GetStarsTransactionsRequest",
    "channels:GetParticipantsRequest",
    "messages:GetSavedDialogsRequest",
]:
    mod, name = item.split(":")
    print(item, inspect.signature(getattr(MODS[mod], name).__init__))

TYPES = """ChannelParticipant ChannelParticipantSelf ChannelParticipantCreator
ChannelParticipantAdmin ChannelParticipantBanned ChannelParticipantLeft
ChannelParticipantsRecent ChannelParticipantsAdmins ChannelParticipantsBots
ChannelParticipantsKicked ChannelParticipantsBanned ChannelParticipantsSearch
ChannelParticipantsContacts ChannelParticipantsMentions ChannelAdminLogEventsFilter
ChannelAdminLogEvent ForumTopic ForumTopicDeleted InputNotifyForumTopic
InputPeerNotifySettings ChatInvitePeek ChatInviteAlready ChatInvite
ChatInviteExported ChatInvitePublicJoinRequests StarsSubscriptionPricing
InputChatUploadedPhoto InputChatPhotoEmpty ChatReactionsAll ChatReactionsNone
ChatReactionsSome ProfileTabPosts InputChannelEmpty InputUserSelf InputUserEmpty
StatsGraphAsync StatsGraph StatsGraphError ChatAdminWithInvites
ChatInviteImporter""".split()
print("MISSING TYPES:", [t for t in TYPES if not hasattr(types, t)])
