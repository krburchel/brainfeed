#!/usr/bin/env python3
"""Build the "Save to BrainFeed" and "Save Photo to BrainFeed" Shortcuts.

    python3 build_shortcut.py TOKEN_FILE OUT_DIR
    shortcuts sign --mode anyone --input OUT_DIR/unsigned-links.shortcut --output "Save to BrainFeed.shortcut"
    shortcuts sign --mode anyone --input OUT_DIR/unsigned-photos.shortcut --output "Save Photo to BrainFeed.shortcut"

TOKEN_FILE holds a BrainFeed API token (64 hex chars) whose sha256 fingerprint
is registered in BrainFeed → Settings → Connected agents. The token is embedded
in the shortcut, so treat the built file like a password: don't share it.

Two shortcuts, so the Share sheet picks the right one by content type with no
branching (a single shortcut using "Get Images from Input" downloaded pictures
from shared web pages instead of saving the link):
  - Save to BrainFeed: links and text (Instagram/Safari shares, selected text) →
    /v1/notes with source "ios". Run it directly to type a quick note.
  - Save Photo to BrainFeed: images (Photos, screenshots) → each converted to
    JPEG and uploaded to /v1/photos as its own "📷 Photo" note.
Both show BrainFeed's reply ("Saved to BrainFeed ✓" or the error) as a notification.
"""
import plistlib
import sys
import uuid

BASE = "https://bzvibdjrknqvmurwjroq.supabase.co/functions/v1/brainfeed-api"


def uid():
    return str(uuid.uuid4()).upper()


def attachment(att):
    return {"Value": att, "WFSerializationType": "WFTextTokenAttachment"}


def text(s):
    return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def text_of(att):
    return {"Value": {"string": "￼", "attachmentsByRange": {"{0, 1}": att}}, "WFSerializationType": "WFTextTokenString"}


def output(u, name):
    return {"Type": "ActionOutput", "OutputUUID": u, "OutputName": name}


def fields(pairs):
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": text(k), "WFValue": v} for k, v in pairs
    ]}, "WFSerializationType": "WFDictionaryFieldValue"}


def action(ident, **params):
    return {"WFWorkflowActionIdentifier": ident, "WFWorkflowActionParameters": params}


def headers_for(token):
    assert len(token) == 64 and all(c in "0123456789abcdef" for c in token), "bad token"
    return fields([("X-BrainFeed-Token", text(token))])


def notify_message(source_uuid, source_name):
    msg = uid()
    return [
        action("is.workflow.actions.getvalueforkey", UUID=msg, WFGetDictionaryValueType="Value",
               WFDictionaryKey="message", WFInput=attachment(output(source_uuid, source_name))),
        action("is.workflow.actions.notification", WFNotificationActionTitle="BrainFeed",
               WFNotificationActionBody=text_of(output(msg, "Dictionary Value")), WFNotificationActionSound=False),
    ]


def workflow(actions, input_classes, ask_for_text):
    wf = {
        "WFWorkflowClientVersion": "3200.0.0",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 1440408063, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": input_classes,
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": actions,
    }
    if ask_for_text:
        wf["WFWorkflowNoInputBehavior"] = {"Name": "WFWorkflowNoInputBehaviorAskForInput",
                                           "Parameters": {"ItemClass": "WFStringContentItem"}}
    return wf


def build_links(token):
    """Links and text (Instagram/Safari shares, selected text). Run directly to type a note."""
    post = uid()
    actions = [
        action("is.workflow.actions.downloadurl", UUID=post, WFURL=BASE + "/v1/notes",
               WFHTTPMethod="POST", WFHTTPBodyType="JSON", ShowHeaders=True, WFHTTPHeaders=headers_for(token),
               WFJSONValues=fields([("body", text_of({"Type": "ExtensionInput"})), ("source", text("ios"))])),
    ] + notify_message(post, "Contents of URL")
    return workflow(actions, ["WFURLContentItem", "WFSafariWebPageContentItem", "WFArticleContentItem",
                              "WFStringContentItem", "WFRichTextContentItem"], ask_for_text=True)


def build_photos(token):
    """Images only (Photos, screenshots). Each becomes its own JPEG "📷 Photo" note."""
    g, converted, post, results = uid(), uid(), uid(), uid()
    actions = [
        action("is.workflow.actions.repeat.each", GroupingIdentifier=g, WFControlFlowMode=0,
               WFInput=attachment({"Type": "ExtensionInput"})),
        action("is.workflow.actions.image.convert", UUID=converted, WFImageFormat="JPEG",
               WFImageCompressionQuality=0.75, WFImagePreserveMetadata=False,
               WFInput=attachment({"Type": "Variable", "VariableName": "Repeat Item"})),
        action("is.workflow.actions.downloadurl", UUID=post, WFURL=BASE + "/v1/photos?source=ios",
               WFHTTPMethod="POST", WFHTTPBodyType="File", ShowHeaders=True, WFHTTPHeaders=headers_for(token),
               WFRequestVariable=attachment(output(converted, "Converted Image"))),
        action("is.workflow.actions.repeat.each", GroupingIdentifier=g, WFControlFlowMode=2, UUID=results),
    ] + notify_message(results, "Repeat Results")
    return workflow(actions, ["WFImageContentItem", "WFPhotoMediaContentItem"], ask_for_text=False)


if __name__ == "__main__":
    token_file, out_dir = sys.argv[1], sys.argv[2]
    with open(token_file) as f:
        tok = f.read().strip()
    for name, builder in (("links", build_links), ("photos", build_photos)):
        path = f"{out_dir}/unsigned-{name}.shortcut"
        with open(path, "wb") as f:
            plistlib.dump(builder(tok), f, fmt=plistlib.FMT_BINARY)
        print(f"wrote {path}")
