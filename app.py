# TODO: Simplify SendMessage and archive it in json format
#------------------------------------套件載入---------------------------------------#
from flask import Flask, request, abort
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent, UnfollowEvent, ImageMessage, ImageSendMessage, TemplateSendMessage , PostbackEvent, QuickReply, QuickReplyButton, LocationAction, LocationMessage, CameraAction, CameraRollAction,
)

from google.cloud import storage
from google.cloud import firestore
import random

# 圖文選單用
from linebot.models import RichMenu
import requests
import json

# FlexSendMessage
from linebot.models import FlexSendMessage
from linebot.models.flex_message import (
    BubbleContainer, ImageComponent
)
from linebot.models.actions import URIAction

# 圖片下載與上傳專用
import urllib.request
import os

# 建立日誌紀錄設定檔
# https://googleapis.dev/python/logging/latest/stdlib-usage.html
import logging
import google.cloud.logging
from google.cloud.logging.handlers import CloudLoggingHandler

#------------------------------------環境變數---------------------------------------#
line_bot_api = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
handler = os.environ["LINE_CHANNEL_SECRET"]
bucket_name = os.environ['USER_INFO_GS_BUCKET_NAME']
lineRichMenuId = os.environ['LINE_RICH_MENU_ID']
# os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'ai01-04-b21f811cd31c.json'

#------------------------------------客製化logging訊息---------------------------------------#
# 啟用log的客戶端
client = google.cloud.logging.Client()

# 建立line event log，用來記錄line event
bot_event_handler = CloudLoggingHandler(client,name="ai04_bot_event")
bot_event_logger=logging.getLogger('ai04_bot_event')
# bot_event_logger.setLevel('INFO' if os.getenv('PORT') else 'ERROR' ) # 使用後無法紀錄一般訊息, 暫時drop.
bot_event_logger.setLevel(logging.INFO)
bot_event_logger.addHandler(bot_event_handler)

# Flask約定俗成的用法: 讓Flask知道route在何處?
app = Flask(__name__)

#------------------------------------設定機器人訪問入口---------------------------------------#
@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    print(body) # 有沒有似乎沒差, 推測: 若有print可能在客製bot_event外也看得到訊息.

    # 消息整個交給bot_event_logger，請它傳回GCP
    bot_event_logger.info(body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError as exception:
        bot_event_logger.error(exception)
        # print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'
#------------------------------------拍照辨識---------------------------------------#
# 同步化
import asyncio
from asyncio import BaseEventLoop
from functools import partial, update_wrapper

class Syncify:

    _loop:BaseEventLoop

    def __init__(self, function, loop = None):
        update_wrapper(self, function)
        self._function = function
        self._loop = loop or asyncio.get_event_loop()
    
    def __call__(self, *args, **kwargs):
        # https://stackoverflow.com/questions/6394511/python-functools-wraps-equivalent-for-classes#answer-17705456
        return self._loop.run_until_complete(partial(self._function, *args, **kwargs)())

# Predictor
from aio_pika.patterns import RPC

class Predictor:
    def __init__(self, rpc:RPC):
        self._rpc = rpc

    def predict(self, messageId):
        return Syncify(self._rpc.call)('predict', kwargs={'messageId': messageId})

# PredictorFactory
from aio_pika import connect_robust, RobustConnection
from aio_pika.patterns import RPC
import os

class PredictorFactory:
    def __init__(self, url:str=os.getenv('AMQP_URL')):
        self._url = url

    def __enter__(self):
        self._connection:RobustConnection = Syncify(connect_robust)(self._url)

    def __exit__(self, exception_type, exception_value, exception_traceback):
        Syncify(self._connection.close)()

    def create(self):
        channel = Syncify(self._connection.channel)()
        rpc = Syncify(RPC.create)(channel)
        return Predictor(rpc)

#------------------------------------圖文選單---------------------------------------#
def get_richmenu(line_user_profile):

    # 將選單綁定到特定用戶身上
    # 取出上面得到的菜單Id及用戶id
    # 要求line_bot_api告知Line，將用戶與圖文選單做綁定
    # https://api.line.me/v2/bot/user/{userId}/richmenu/{richMenuId}

    line_bot_api.link_rich_menu_to_user(line_user_profile.user_id, lineRichMenuId)

#------------------------------------firestore讀取寫入---------------------------------------#
# 讀取
def db_read(line_user_profile):
    db = firestore.Client()
    doc_ref = db.collection(u'line-user').document(line_user_profile.user_id)
    doc = doc_ref.get()
    return doc

# 新增
def db_add(line_user_profile, user_dict):
    db = firestore.Client()
    doc_ref = db.collection(u'line-user').document(line_user_profile.user_id)
    doc_ref.set(user_dict)

# 產生第一筆用戶user_dict
def first_user_dict(line_user_profile):
    user_dict={
        "user_id":line_user_profile.user_id,
        "picture_url": line_user_profile.picture_url,
        # 照片存取權設公開後會產生的網址
        "picture_url_public": f"https://storage.googleapis.com/{bucket_name}/{line_user_profile.user_id}/user_pic.png",
        "display_name": line_user_profile.display_name,
        "status_message": line_user_profile.status_message,
        "line_user_system_language": line_user_profile.language,
        "blocked": False,
        "latitude": "",
        "longitude": ""
    }
    return user_dict

#------------------------------------QuickReply---------------------------------------#
## 設計QuickReplyButton的List
#-----尋找附近菜市場與漁港-----#
location_quick_list = QuickReply(
    items = [QuickReplyButton(action=LocationAction(label="上傳位置"))]
)

#-----拍照辨識-----#
# 點擊後切換至照片相簿選擇
cameraRollQRB = QuickReplyButton(action=CameraRollAction(label="上傳照片"))

# 開啟相機鏡頭
cameraQRB = QuickReplyButton(action=CameraAction(label="拍攝照片"))

# 合併
photo_quick_list = QuickReply(
    items = [cameraRollQRB, cameraQRB]
)

#------------------------------------單個Flex設定區---------------------------------------#
# -----美味料理----- #
# fm1: 選擇食譜
fm1_m = FlexSendMessage(
    alt_text='選擇食譜',
    contents={
  "type": "bubble",
  "body": {
    "type": "box",
    "layout": "vertical",
    "contents": [
      {
        "type": "image",
        "url": "https://i.imgur.com/rTHVZTn.png",
        "size": "full",
        "aspectRatio": "1:1",
        "aspectMode": "cover",
        "gravity": "center"
      },
      {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "postback",
              "data": "{'class1': 'CookMenu', 'class2': '', 'button': 'OctopusCookbook'}",
              "text": "@章魚食譜",
              "label": "章魚"
            },
            "margin": "xxl",
            "offsetTop": "sm"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "魷魚",
              "data": "{'class1': 'CookMenu', 'class2': '', 'button': 'SquidCookbook'}",
              "text": "@魷魚食譜"
            },
            "style": "link",
            "height": "sm"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "透抽 (中卷)",
              "data": "{'class1': 'CookMenu', 'class2': '', 'button': 'NeriticSquidCookbook'}",
              "text": "@透抽食譜"
            },
            "style": "link",
            "height": "sm"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "花枝 (烏賊)",
              "data": "{'class1': 'CookMenu', 'class2': '', 'button': 'CuttlefishCookbook'}",
              "text": "@花枝食譜"
            },
            "style": "link",
            "height": "sm"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "軟絲",
              "data": "{'class1': 'CookMenu', 'class2': '', 'button': 'BigfinSquidCookbook'}",
              "text": "@軟絲食譜"
            },
            "style": "link",
            "height": "sm"
          }
        ],
        "position": "absolute",
        "width": "100%",
        "paddingAll": "xxl",
        "offsetTop": "xxl"
      }
    ],
    "paddingAll": "0px"
  }
}
)

#-----拍照辨識-----#
fm2_m = FlexSendMessage(
        alt_text='拍照辨識選單',
        contents={
  "type": "bubble",
  "hero": {
    "type": "image",
    "url": "https://i.imgur.com/R0mTvto.jpg",
    "size": "full",
    "aspectRatio": "20:13",
    "aspectMode": "cover",
    "action": {
      "type": "uri",
      "uri": "http://linecorp.com/"
    }
  },
  "body": {
    "type": "box",
    "layout": "vertical",
    "contents": [
      {
        "type": "text",
        "text": "頭足類辨識",
        "weight": "bold",
        "size": "xl"
      },
      {
        "type": "box",
        "layout": "vertical",
        "margin": "lg",
        "spacing": "sm",
        "contents": [
          {
            "type": "box",
            "layout": "baseline",
            "spacing": "sm",
            "contents": [
              {
                "type": "text",
                "text": "支援",
                "color": "#aaaaaa",
                "size": "sm",
                "flex": 1
              },
              {
                "type": "text",
                "text": "章魚、魷魚、透抽、花枝、軟絲",
                "wrap": True,
                "color": "#666666",
                "size": "sm",
                "flex": 5
              }
            ]
          },
          {
            "type": "box",
            "layout": "baseline",
            "spacing": "sm",
            "contents": [
              {
                "type": "text",
                "text": "建議",
                "color": "#ab117f",
                "size": "sm",
                "flex": 1
              },
              {
                "type": "text",
                "text": "單一照片僅含單種類型",
                "wrap": True,
                "color": "#ab117f",
                "size": "sm",
                "flex": 5
              }
            ]
          },
          {
            "type": "box",
            "layout": "baseline",
            "spacing": "sm",
            "contents": [
              {
                "type": "text",
                "text": "注意",
                "color": "#aaaaaa",
                "size": "sm",
                "flex": 1
              },
              {
                "type": "text",
                "text": "處理過的頭足類、非頭足類照片、一次多張照片，可能會有識別結果或順序上的錯誤",
                "wrap": True,
                "color": "#666666",
                "size": "sm",
                "flex": 5
              }
            ]
          }
        ]
      }
    ]
  }
},quick_reply=photo_quick_list
)

# -----地圖搜尋結果----- #
def map_search(market_map_url, fish_map_url):
    fm3_m = FlexSendMessage(
            alt_text='地圖搜尋結果',
            contents={
    "type": "bubble",
    "hero": {
        "type": "image",
        "url": "https://i.imgur.com/DGG2ikn.png",
        "size": "4xl",
        "aspectMode": "fit",
        "action": {
        "type": "uri",
        "uri": "http://linecorp.com/"
        }
    },
    "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
        {
            "type": "text",
            "text": "地圖搜尋結果",
            "weight": "bold",
            "size": "xl",
            "align": "center"
        }
        ]
    },
    "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
        {
            "type": "button",
            "height": "sm",
            "action": {
            "type": "uri",
            "label": "附近菜市場",
            "uri": market_map_url
            },
            "style": "primary",
            "color": "#1809e8"
        },
        {
            "type": "button",
            "height": "sm",
            "action": {
            "type": "uri",
            "label": "附近漁港",
            "uri": fish_map_url
            }
        },
        {
            "type": "box",
            "layout": "vertical",
            "contents": [],
            "margin": "sm"
        }
        ],
        "flex": 0
    }
    }
    )
    return fm3_m

#------------------------------------多個Flex設定區---------------------------------------#
# Flex框架
# fm_mix = FlexSendMessage(
#         alt_text='文字訊息',
#         contents={}
# )

#-----想知道更多-----#
fm_mix_more = FlexSendMessage(
        alt_text='想知道更多',
        contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectRatio": "20:13",
        "aspectMode": "cover",
        "url": "https://greenblob.azureedge.net/upload/News_3032/201901040919021.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "text",
            "text": "「魷魚、透抽、花枝、軟絲、章魚」到底怎麼分",
            "wrap": True,
            "weight": "bold",
            "size": "xl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "button",
            "flex": 2,
            "style": "primary",
            "action": {
              "type": "uri",
              "label": "無毒農網站",
              "uri": "https://greenbox.tw/Blog/BlogPostNew/6191/"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "解析海鮮",
              "uri": "https://www.9900.com.tw/talk/BBSShowV2.aspx?jid=03b318e55254004ff947"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "搞懂頭足類家族",
              "uri": "https://www.travelrich.com.tw/news/foodnews/foodnews5596.html"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "如何辨別頭足類",
              "uri": "https://fae.coa.gov.tw/food_item.php?type=AS01&id=183"
            }
          }
        ]
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectRatio": "20:13",
        "aspectMode": "cover",
        "url": "https://t4.ftcdn.net/jpg/00/95/91/39/240_F_95913998_FK6u9kOXquNL5XWRl6UW7uVEvEtdhRlm.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "text",
            "text": "章魚",
            "wrap": True,
            "weight": "bold",
            "size": "5xl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "none",
        "contents": [
          {
            "type": "button",
            "style": "primary",
            "action": {
              "type": "postback",
              "label": "詳細介紹章魚",
              "data": "{'class1': 'MoreInfo', 'class2': 'Octopus', 'button': 'OctopusInfo'}",
              "text": "@詳細介紹章魚"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "料理章魚前處理",
              "uri": "https://food.ltn.com.tw/article/519"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "章魚料理食譜",
              "data": "{'class1': 'MoreInfo', 'class2': 'Octopus', 'button': 'OctopusCookbook'}",
              "text": "@章魚食譜"
            }
          }
        ]
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": "https://img.ltn.com.tw/Upload/food/page/2015/02/12/150212-299-2-Tlipx.png",
        "size": "full",
        "aspectRatio": "20:13",
        "aspectMode": "cover"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "魷魚",
            "size": "5xl",
            "weight": "bold"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "詳細介紹魷魚",
              "data": "{'class1': 'MoreInfo', 'class2': 'Squid', 'button': 'SquidInfo'}",
              "text": "@詳細介紹魷魚"
            },
            "style": "primary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "料理魷魚前處理",
              "uri": "https://food.ltn.com.tw/article/5267"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "魷魚料理食譜",
              "data": "{'class1': 'MoreInfo', 'class2': 'Squid', 'button': 'SquidCookbook'}",
              "text": "@魷魚食譜"
            }
          }
        ]
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": "https://img.ltn.com.tw/Upload/food/page/2015/02/12/150212-299-4-u2yo8.png",
        "size": "full",
        "aspectRatio": "20:13",
        "aspectMode": "cover"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "透抽",
            "size": "5xl",
            "weight": "bold"
          },
          {
            "type": "text",
            "text": "小管/小卷/中卷/鎖管",
            "size": "xl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "詳細介紹透抽",
              "data": "{'class1': 'MoreInfo', 'class2': 'NeriticSquid', 'button': 'NeriticSquidInfo'}",
              "text": "@詳細介紹透抽"
            },
            "style": "primary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "料理透抽前處理",
              "uri": "https://food.ltn.com.tw/article/1246/2"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "透抽料理食譜",
              "data": "{'class1': 'MoreInfo', 'class2': 'NeriticSquid', 'button': 'NeriticSquidCookbook'}",
              "text": "@透抽食譜"
            }
          }
        ]
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": "https://img.ltn.com.tw/Upload/food/page/2015/02/12/150212-299-05-TKyvL.png",
        "aspectRatio": "20:13",
        "aspectMode": "cover",
        "size": "full"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "花枝",
            "weight": "bold",
            "size": "5xl"
          },
          {
            "type": "text",
            "text": "烏賊/墨魚",
            "size": "xl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "詳細介紹花枝",
              "data": "{'class1': 'MoreInfo', 'class2': 'Cuttlefish', 'button': 'CuttlefishInfo'}",
              "text": "@詳細介紹花枝"
            },
            "style": "primary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "料理花枝前處理",
              "uri": "https://food.ltn.com.tw/article/4574"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "花枝料理食譜",
              "data": "{'class1': 'MoreInfo', 'class2': 'Cuttlefish', 'button': 'CuttlefishCookbook'}",
              "text": "@花枝食譜"
            }
          }
        ]
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": "https://img.ltn.com.tw/Upload/food/page/2015/02/12/150212-299-1-ttMdo.png",
        "size": "full",
        "aspectRatio": "20:13",
        "aspectMode": "cover"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "軟絲",
            "weight": "bold",
            "size": "5xl"
          },
          {
            "type": "text",
            "text": "擬烏賊/軟翅仔",
            "size": "xl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "詳細介紹軟絲",
              "data": "{'class1': 'MoreInfo', 'class2': 'BigfinSquid', 'button': 'BigfinSquidInfo'}",
              "text": "@詳細介紹軟絲"
            },
            "style": "primary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "料理軟絲前處理",
              "uri": "https://food.ltn.com.tw/article/1382"
            }
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "軟絲料理食譜",
              "data": "{'class1': 'MoreInfo', 'class2': 'BigfinSquid', 'button': 'BigfinSquidCookbook'}",
              "text": "@軟絲食譜"
            }
          }
        ]
      }
    }
  ]
}
)
#-----美味料理-----#
# 章魚食譜
fm_mix_cook1 = FlexSendMessage(
    alt_text='章魚食譜',
    contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/gy51-016b.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "百里香炒小章魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0347"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk47-011.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "涼拌小章魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0544"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk137-043.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "五味章魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0250"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk67-029b.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "黃金滷汁燜章魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-084"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk26-139.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "義式涼拌章魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-070"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://i.imgur.com/N0FmWHq.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "查看更多",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#ab117f"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "食譜自由配",
                    "wrap": True,
                    "color": "#000000",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://food.ltn.com.tw/search?t=ingr&q=%E7%AB%A0%E9%AD%9A"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#ffc841",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    }
  ]
}
)

# 魷魚食譜
fm_mix_cook2 = FlexSendMessage(
        alt_text='魷魚食譜',
        contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/gy30-042.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "沙茶魷魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0293"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/hq27-022.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "炸魷魚鬚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-109"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk119-013a.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "芹菜炒魷魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0207"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/gy30-040.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "宮保魷魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0291"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk189-046a.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "水煮魷魚",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0428"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://i.imgur.com/N0FmWHq.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "查看更多",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#ab117f"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "食譜自由配",
                    "wrap": True,
                    "color": "#000000",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://food.ltn.com.tw/search?t=ingr&q=%E9%AD%B7%E9%AD%9A"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#ffc841",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    }
  ]
}
)

# 透抽食譜
fm_mix_cook3 = FlexSendMessage(
        alt_text='透抽食譜',
        contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk164-014a.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "三杯透抽",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0315"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk058-089c.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "三色透抽卷",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0571"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/yqn35-030.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "西芹透抽炒",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0411"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/gy62-058.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "海鮮蕃茄湯",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B01-1134"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk194a-009.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "韓式海鮮煎餅",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0519"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://i.imgur.com/N0FmWHq.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "查看更多",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#ab117f"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "食譜自由配",
                    "wrap": True,
                    "color": "#000000",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://food.ltn.com.tw/search?t=ingr&q=%E9%80%8F%E6%8A%BD"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#ffc841",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    }
  ]
}
)

# 花枝食譜
fm_mix_cook4 = FlexSendMessage(
        alt_text='花枝食譜',
        contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/yqn48-041a.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "台式炒花枝",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0441"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/yqn32-010.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "海鮮粥",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0415"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk39-111b.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "生炒花枝",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-130"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk062-058.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "燴三鮮",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0582"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/yqn50-043b.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "宮保花枝煲",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0455"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://i.imgur.com/N0FmWHq.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "查看更多",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#ab117f"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "食譜自由配",
                    "wrap": True,
                    "color": "#000000",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://food.ltn.com.tw/search?t=ingr&q=%E8%8A%B1%E6%9E%9D"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#ffc841",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    }
  ]
}
)

# 軟絲食譜
fm_mix_cook5 = FlexSendMessage(
        alt_text='軟絲食譜',
        contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk119-012a.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "炒軟絲",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0205"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/yqn077-035.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "椒鹽炸軟絲",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0522"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk098-091a.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "花椰菜炒軟絲",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0670"
                    },
                    "align": "center"
                  }
                ],
                "cornerRadius": "5px",
                "backgroundColor": "#005555"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/whk103-014.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "鮮味軟絲",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0700"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://d3l76hx23vw40a.cloudfront.net/recipe/bk137-032.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "麻辣軟絲",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#205375"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "料理食譜",
                    "wrap": True,
                    "color": "#EDE6DB",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://www.ytower.com.tw/recipe/iframe-recipe.asp?seq=B04-0247"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#005555",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    },
    {
      "type": "bubble",
      "size": "micro",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectMode": "cover",
        "aspectRatio": "320:213",
        "url": "https://i.imgur.com/N0FmWHq.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "查看更多",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
            "color": "#ab117f"
          },
          {
            "type": "box",
            "layout": "vertical",
            "contents": [
              {
                "type": "box",
                "layout": "baseline",
                "spacing": "sm",
                "contents": [
                  {
                    "type": "text",
                    "text": "食譜自由配",
                    "wrap": True,
                    "color": "#000000",
                    "size": "lg",
                    "flex": 5,
                    "action": {
                      "type": "uri",
                      "label": "action",
                      "uri": "https://food.ltn.com.tw/search?t=ingr&q=%E8%BB%9F%E7%B5%B2"
                    },
                    "align": "center"
                  }
                ],
                "backgroundColor": "#ffc841",
                "cornerRadius": "5px"
              }
            ]
          }
        ],
        "spacing": "sm",
        "paddingAll": "13px",
        "backgroundColor": "#EFEFEF"
      }
    }
  ]
}
)

#-----參考資訊-----#
fm_mix_refer = FlexSendMessage(
        alt_text='參考資訊',
        contents={
  "type": "carousel",
  "contents": [
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectRatio": "20:14",
        "aspectMode": "cover",
        "url": "https://i.imgur.com/QNNChkC.jpg"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "text",
            "text": "尋找菜市場、漁港",
            "wrap": True,
            "size": "xxl",
            "weight": "bold",
            "style": "normal",
            "decoration": "underline"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "查詢附近菜市場、漁港位置",
              "data": "{'class1': 'ReferInfo', 'class2': 'FindMarket', 'button': 'OpenPosition'}"
            },
            "style": "primary",
            "color": "#1809e8",
            "offsetEnd": "none",
            "offsetTop": "xxl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [],
        "offsetBottom": "xxl"
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "size": "full",
        "aspectRatio": "20:14",
        "aspectMode": "cover",
        "url": "https://i.imgur.com/PM0BJXh.png"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "text",
            "text": "常見的頭足類問題",
            "wrap": True,
            "weight": "bold",
            "size": "xl"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "墨斗仔、小章魚是一樣的嗎？",
              "data": "{'class1': 'ReferInfo', 'class2': 'CommonProblem', 'button': 'CQ1'}",
              "text": "@墨斗仔、小章魚是一樣的嗎？"
            },
            "style": "secondary"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "小管、小卷、透抽是一樣的嗎？",
              "data": "{'class1': 'ReferInfo', 'class2': 'CommonProblem', 'button': 'CQ2'}",
              "text": "@小管、小卷、透抽是一樣的嗎？"
            },
            "style": "secondary"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "烏賊就是花枝嗎？",
              "data": "{'class1': 'ReferInfo', 'class2': 'CommonProblem', 'button': 'CQ3'}",
              "text": "@烏賊就是花枝嗎？"
            },
            "style": "secondary"
          },
          {
            "type": "button",
            "action": {
              "type": "postback",
              "label": "軟絲和我們平常吃的鎖管關係？",
              "data": "{'class1': 'ReferInfo', 'class2': 'CommonProblem', 'button': 'CQ4'}",
              "text": "@軟絲和我們平常吃的鎖管關係？"
            },
            "style": "secondary"
          }
        ]
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": "https://i.imgur.com/nUlEpoG.jpg",
        "size": "full",
        "aspectRatio": "20:14",
        "backgroundColor": "#9fa5a5"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "推薦書籍",
            "size": "xl",
            "weight": "bold"
          }
        ],
        "spacing": "none",
        "borderWidth": "none"
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "臺灣產頭足類動物圖鑑",
              "uri": "https://gpi.culture.tw/books/1010600726"
            },
            "color": "#1809e8",
            "margin": "none"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "頭足類完全識別圖解",
              "uri": "https://www.books.com.tw/products/0010589767"
            },
            "color": "#1809e8"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "海洋博物誌",
              "uri": "https://www.books.com.tw/products/0010867472"
            },
            "color": "#1809e8"
          }
        ],
        "offsetBottom": "xxl"
      }
    },
    {
      "type": "bubble",
      "hero": {
        "type": "image",
        "url": "https://i.imgur.com/WhRMhX9.jpg",
        "size": "full",
        "aspectRatio": "20:14"
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "查看更多資訊",
            "size": "xl",
            "weight": "bold"
          }
        ]
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "頭足類動物對神經科學研究的影響",
              "uri": "https://www.scimonth.com.tw/archives/5261"
            },
            "style": "secondary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "世界上的10種頭足類動物",
              "uri": "https://read01.com/zh-tw/5n78Jk4.html#.YuJ-EnZBw2w"
            },
            "style": "secondary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "頭足類之營養價值",
              "uri": "https://life.anyongfresh.com/%E9%80%8F%E6%8A%BD-nutrients/"
            },
            "style": "secondary"
          },
          {
            "type": "button",
            "action": {
              "type": "uri",
              "label": "小卷盛產季節",
              "uri": "https://www.taitung-dessertgirl-blog.tw/neritic-squid/"
            },
            "style": "secondary"
          }
        ],
        "spacing": "sm"
      }
    }
  ]
}
)

#------------------------------------handler功能區---------------------------------------#
# 功能1: 關注取用戶資料
@handler.add(FollowEvent)
def handle_follow_event(event):

    # 取個資
    line_user_profile= line_bot_api.get_profile(event.source.user_id)

    # -----1.Cloud Storage----- #
    # 跟line 取回照片，並放置在本地端
    file_name = line_user_profile.user_id+'.jpg'
    urllib.request.urlretrieve(line_user_profile.picture_url, file_name)

    # 設定內容
    storage_client = storage.Client()
    destination_blob_name=f"{line_user_profile.user_id}/user_pic.png"
    source_file_name=file_name
       
    # 進行上傳
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(source_file_name)

    # 移除本地檔案
    os.remove(file_name)

    # -----2.Firestore----- #
    # 讀取用戶資料
    doc = db_read(line_user_profile)
    # 確認資料是否存在
    if doc.exists:
        user_dict = doc.to_dict()
        # 更新資訊, 除了位置資訊與大頭貼url.
        user_dict["user_id"] = line_user_profile.user_id
        user_dict["display_name"] = line_user_profile.display_name
        user_dict["status_message"] = line_user_profile.status_message
        user_dict["line_user_system_language"] = line_user_profile.language
        user_dict["blocked"] = False
        db_add(line_user_profile, user_dict)
    else:
        user_dict = first_user_dict(line_user_profile)
        db_add(line_user_profile, user_dict)

    # -----3.綁定圖文選單----- #
    get_richmenu(line_user_profile)

    # -----4.開場白----- #
    line_bot_api.reply_message(
        event.reply_token, [
        TextSendMessage(str(line_user_profile.display_name)+" 您好\U0001F44F\n\n每年一到夏季是小卷、花枝、透抽的盛產季節。\n看到一籃籃澎湃的頭足類海鮮是不是分不清楚誰是誰呢？"),
        TextSendMessage("我是AI頭足類辨識機器人\n會協助您進行辨識 \U0001F4F8\n\n您可以透過點擊下方按鈕\n「拍照辨識」進行辨識！\n帶您了解如何挑選新鮮漁獲\n以及如何處理牠\U0001F52A"),
        TextSendMessage("另外您也可以透過\n「參考資訊」\n「美味料理」\n「想知道更多？」\n\U0001F50D 查詢更多頭足類資訊、美味食譜和簡單的外觀辨識小撇步。"),
        TextSendMessage("讓我們一起開始探索吧！"),
        ImageSendMessage(
            original_content_url='https://i.imgur.com/N0FmWHq.jpg',
            preview_image_url='https://i.imgur.com/N0FmWHq.jpg'),
        ]
    )

# 功能1-1: 封鎖後更新資料庫
@handler.add(UnfollowEvent)
def handle_line_unfollow(event):

    # 讀取用戶資料
    # line_user_profile = line_bot_api.get_profile(event.source.user_id) # ERROR: 無法讀資料
    # doc = db_read(line_user_profile)
    db = firestore.Client()
    doc_ref = db.collection(u'line-user').document(event.source.user_id)
    doc = doc_ref.get()

    # 確認資料是否存在
    if doc.exists:
        user_dict = doc.to_dict()
        # 更新封鎖狀態
        user_dict["blocked"] = True
        # db_add(line_user_profile, user_dict)
        doc_ref.set(user_dict)
    else:    
        pass


# 功能2: 文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):

    # -----內部檢查-----#
    # 檢查圖文選單id數量
    if(event.message.text=="@檢查圖文"):
        # 取出所有圖文列表
        rich_menu_list = line_bot_api.get_rich_menu_list()

        # 只存放id
        rich_id_list = []
        for rich_menu in rich_menu_list:
            # print(rich_menu.rich_menu_id)
            rich_id_list.append(rich_menu.rich_menu_id)

        # 回傳總數與id清單
        line_bot_api.reply_message(
            event.reply_token, [
                TextSendMessage("圖文選單id數量: "+str(len(rich_menu_list))),
                TextSendMessage(str(rich_id_list)),        
            ]
        )

    # -----0.圖文選單按鈕-----#
    elif(event.message.text=="@參考資訊"):
        line_bot_api.reply_message(event.reply_token, fm_mix_refer)
    elif(event.message.text=="@拍照辨識"):
        line_bot_api.reply_message(event.reply_token, fm2_m)
    elif(event.message.text=="@美味料理"):
        line_bot_api.reply_message(event.reply_token, fm1_m)
    elif(event.message.text=="@想知道更多"):
        line_bot_api.reply_message(event.reply_token, fm_mix_more)

    # -----1.美味料理按鈕-----#
    elif(event.message.text=="@章魚食譜"):
        line_bot_api.reply_message(event.reply_token, fm_mix_cook1)
    elif(event.message.text=="@魷魚食譜"):
        line_bot_api.reply_message(event.reply_token, fm_mix_cook2)
    elif(event.message.text=="@透抽食譜"):
        line_bot_api.reply_message(event.reply_token, fm_mix_cook3)
    elif(event.message.text=="@花枝食譜"):
        line_bot_api.reply_message(event.reply_token, fm_mix_cook4)
    elif(event.message.text=="@軟絲食譜"):
        line_bot_api.reply_message(event.reply_token, fm_mix_cook5)

    # -----2.常見頭足類問題按鈕-----#
    elif(event.message.text=="@墨斗仔、小章魚是一樣的嗎？"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("墨斗仔 即是 耳烏賊\n．頭足綱 Cephalopoda\n．耳烏賊目 Sepiolida\n有餐館把牠叫做「小章魚」，只是長得像，「墨斗仔」不是章魚。\n\n❍ 如何分辨墨斗仔、小章魚?\n墨斗仔: 「假．小章魚」腳很短\n猴水仔: 「真．小章魚」腳較長\n\n❍ 食用墨斗仔種類\n貝瑞氏四盤耳烏賊\n四盤耳烏賊\n雙乳突南方羅素耳烏賊\n\n❍ 更多內容\nhttps://www.facebook.com/taigikho/photos/a.2613680945352541/2661571280563507/?type=3"),
            ]
        )
    elif(event.message.text=="@小管、小卷、透抽是一樣的嗎？"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("小管、小卷、透抽、中卷、鎖管等其實都是同樣的，\n主要是真鎖管和台灣鎖管。\n\n因為各地稱呼不同，有人就會把較小隻的鎖管稱為小管，\n體型較大的稱為中卷或是透抽，\n更大的甚至叫砲管，\n但其實他們都是一樣的。\n\n基本上是不同體長個體的稱呼，\n通常15公分內較小隻的就是小卷，15公分以上的是中卷。\n\n❍ 更多內容\nhttps://greenbox.tw/Blog/BlogPostNew/6191\nhttps://food.ltn.com.tw/article/1246"),
            ]
        )
    elif(event.message.text=="@烏賊就是花枝嗎？"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("烏賊、墨魚俗稱花枝，\n但不是所有烏賊都叫花枝，\n只有「虎斑烏賊」才是正港的花枝。\n\n在台灣，「花枝」是烏賊(墨魚)的俗名，甚至是代名詞，\n而且是「國台語雙聲帶」(台語音hue-ki，華語音花枝)。\n\n❍ 更多內容\nhttps://www.pinsin.com.tw/blogs/%E6%B5%B7%E7%94%A2%E9%AE%AE%E8%A3%9C/56680"),
            ]
        )
    elif(event.message.text=="@軟絲和我們平常吃的鎖管關係？"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("在分類上，軟絲和鎖管一樣屬於槍鎖管科，兩者的關係比花枝（烏賊、墨魚）還要親近。\n\n軟翅仔(軟絲)雖然屬鎖管科，外型卻較像烏賊科的烏賊。\n軟翅仔的肉鰭不像其他鎖管只在尾部，而是跟烏賊一樣延伸到全身。\n軟翅仔被歸為鎖管科，主要是體內有跟其他鎖管一樣的長條形軟殼，而不是烏賊體內船形的硬殼。\n\n❍ 更多內容\nhttps://fae.coa.gov.tw/food_item.php?type=AS01&id=183\nhttps://smiletaiwan.cw.com.tw/article/1659\n"),
            ]
        )

    # -----3.想知道更多-詳細介紹-----#
    elif(event.message.text=="@詳細介紹章魚"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("·常見品種\n短蛸、長蛸、真蛸\n\n·外型\n章魚僅有8隻腕，腕上的吸盤沒有柄，沒有齒環，也沒有內殼，身體柔軟。"),
            ]
        )
    elif(event.message.text=="@詳細介紹魷魚"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("·常見品種\n烤魷魚、魷魚乾等大多是遠洋捕撈的阿根廷魷、美洲大赤魷、西北太西洋赤魷\n\n·外型\n與鎖管一樣有10隻附肢，其中8隻是腕，另2隻則是具有伸縮性的觸腕，\n身體是圓筒型，但是鰭長不超過身體的一半，\n通常比較大隻，其眼睛沒有薄膜覆蓋，具有趨光性。"),
            ]
        )
    elif(event.message.text=="@詳細介紹透抽"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("·常見品種\n主要以劍尖槍烏賊 (俗稱透抽) 及中國槍烏賊 (俗稱台灣鎖管) 居多\n\n·外型\n十隻腳，其中包括八隻腕足及兩隻觸腕，且各腕具有2列帶柄吸盤，而吸盤不會變形成鈎狀構造。\n身體圓筒型且稍微瘦長，頭頂較尖。\n鰭長超過身體的一半，體內有半透明的基丁質鞘。"),
            ]
        )
    elif(event.message.text=="@詳細介紹花枝"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("·常見品種\n虎斑烏賊、真烏賊、擬目烏賊\n\n·產季\n4月～9月\n\n·外型\n體內有一片白白的、類似衝浪板的殼；\n且身體橢圓、有點胖胖，兩側的鰭像裙襬一樣圍繞整身軀。"),
            ]
        )
    elif(event.message.text=="@詳細介紹軟絲"):
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("·常見品種\n萊氏擬烏賊 \n\n·產季\n4月～9月\n\n·外型\n他和花枝長的很像，但身體比較瘦長，沒有烏賊這麼橢圓，鰭也比較偏菱形，\n從頭至尾有兩片寬大相鄰的肉鰭，身體裡也沒有像烏賊那樣的石灰質硬殼。"),
            ]
        )
    
    # -----其他回覆-----#
    else:
        line_bot_api.reply_message(
            event.reply_token, [
            TextSendMessage("您可點選下方圖文選單選擇想使用的功能"),
            ]
        )

# 功能3: 收到圖片
@handler.add(MessageEvent,ImageMessage)
def handle_line_image(event):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        predictorFactory = PredictorFactory()
        with predictorFactory:
            predictor = predictorFactory.create()
            ret = predictor.predict(event.message.id)
        
        if ret == "octopus":
            line_bot_api.reply_message(
                event.reply_token, [
                TextSendMessage("【章魚】- AI辨識結果\n\n❍ 生物分類\n·頭足綱 Cephalopoda\n·八腕目 Octopoda\n\n❍ 如何挑選新鮮的章魚❓\n✔ 體表色澤及眼睛明亮\n✔ 體液黏稠\n✔ 足部吸盤沒有脫落\n✔ 輕拍觸腕，觸腕上的吸孔會快速收縮閉合，代表活動力強\n\n❍ 料理前要如何處理❓\nhttps://food.ltn.com.tw/article/519"),
                ]
            )
        elif ret == "squid":
            line_bot_api.reply_message(
                event.reply_token, [
                TextSendMessage("【魷魚】- AI辨識結果\n\n❍ 生物分類\n·頭足綱 Cephalopoda\n·開眼魷目 Oegopsida\n\n❍ 如何挑選新鮮的魷魚❓\n✔ 水發魷魚是加工發泡，購買時得要留意是否有藥水殘留的味道\n✔ 乾魷魚挑選上頭有覆蓋有白色粉狀結晶，表示其風乾過程較天然\n✔ 新鮮魷魚則是表面富有彈性且外皮色澤深，不具腥味\n\n❍ 料理前要如何處理❓\nhttps://food.ltn.com.tw/article/5267"),
                ]
            )
        elif ret == "cuttlefish":
            line_bot_api.reply_message(
                event.reply_token, [
                TextSendMessage("【花枝】- AI辨識結果\n·烏賊\n·墨魚\n\n❍ 生物分類\n·頭足綱 Cephalopoda\n·烏賊目 Sepiida\n\n❍ 如何挑選新鮮的花枝❓\n✔ 眼睛明亮\n✔ 表皮完整且具有光澤、透明感、緊實富彈性的觸感\n✔ 背部的骨板完整\n✔ 觸角吸盤具黏性\n✔ 聞起來沒有腥臭味\n\n❍ 料理前要如何處理❓\nhttps://food.ltn.com.tw/article/4574"),
                ]
            )
        elif ret == "bigfin squid":
            line_bot_api.reply_message(
                event.reply_token, [
                TextSendMessage("【軟絲】- AI辨識結果\n·擬烏賊\n·軟翅仔\n\n❍ 生物分類\n·頭足綱 Cephalopoda\n·閉眼魷目 Myopsida\n\n❍ 如何挑選新鮮的軟絲❓\n✔ 眼睛明亮\n✔ 表皮完整度無破損\n✔ 手指輕壓外皮，具有彈性\n\n❍ 料理前要如何處理❓\nhttps://food.ltn.com.tw/article/1382"),
                ]
            )
        elif ret == "neritic squid":
            line_bot_api.reply_message(
                event.reply_token, [
                TextSendMessage("【透抽】- AI辨識結果\n·小管\n·小卷\n·中卷\n·鎖管\n\n❍ 生物分類\n·頭足綱 Cephalopoda\n·閉眼魷目 Myopsida\n\n❍ 如何挑選新鮮的透抽❓\n✔ 表皮完整且具有光澤\n✔ 鎖管的膜完整\n✔ 新鮮小卷的表面會有一層粉紅色的薄膜，肉質白皙、近乎透明，摸起來彈性佳\n✔ 透抽身體是否異常鼓起，有可能肚子裡還有未消化的小魚，或是不肖的業者將小魚填充至體內，使得購買時重量增加\n\n❍ 料理前要如何處理❓\nhttps://food.ltn.com.tw/article/1246/2"),
                ]
            )
        else:
            pass
    except Exception as exception:
        bot_event_logger.error(exception)
        line_bot_api.reply_message(
            event.reply_token, [
                TextSendMessage("辨識服務未開啟，請聯繫管理員。"),
            ]
        )

    # 取出照片
    image_blob = line_bot_api.get_message_content(event.message.id)
    temp_file_path=f"""{event.message.id}.png"""
    
    with open(temp_file_path, 'wb') as fd:
        for chunk in image_blob.iter_content():
            fd.write(chunk)

    # 上傳至bucket
    storage_client = storage.Client()
    destination_blob_name = f'{event.source.user_id}/image/{event.message.id}.png'
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(temp_file_path)

    # 刪除暫存照片
    os.remove(temp_file_path)

# 功能4: PostbackEvent
@handler.add(PostbackEvent)
def handle_post_message(event):
    if event.postback.data == "{'class1': 'ReferInfo', 'class2': 'FindMarket', 'button': 'OpenPosition'}":
        line_bot_api.reply_message(
                event.reply_token, [
                    TextSendMessage(text='等待位置資訊中', quick_reply=location_quick_list)                   
                ] 
            )

# 功能5: 收到位置消息
@handler.add(MessageEvent, message=LocationMessage)
def handle_location_message(event):
    # address = event.message.address
    latitude = event.message.latitude
    longitude = event.message.longitude
    market_map_url = f'https://www.google.com.tw/maps/search/%E8%8F%9C%E5%B8%82%E5%A0%B4/@{latitude},{longitude},15.75z?hl=zh-TW'
    fish_map_url = f'https://www.google.com.tw/maps/search/%E6%BC%81%E6%B8%AF/@{latitude},{longitude},15z?hl=zh-TW'
    line_bot_api.reply_message(
        event.reply_token, [
            # TextSendMessage("地圖搜尋結果如下，\n請點選按鈕開啟地圖。"),
            map_search(market_map_url, fish_map_url),
        ]
    )

    # -----儲存用戶最後位置----- #
    line_user_profile = line_bot_api.get_profile(event.source.user_id)
    # 讀取用戶資料
    doc = db_read(line_user_profile)
    # 確認資料是否存在
    if doc.exists:
        user_dict = doc.to_dict()
        # 更新位置資訊
        user_dict["latitude"] = str(latitude)
        user_dict["longitude"] = str(longitude)
        db_add(line_user_profile, user_dict)
    else:    
        user_dict = first_user_dict(line_user_profile)
        # 更新位置資訊
        user_dict["latitude"] = str(latitude)
        user_dict["longitude"] = str(longitude)
        db_add(line_user_profile, user_dict)

    # 想用logging紀錄位置, 但不會用.
    # bot_event_logger.info(f"latitude: {latitude}, longitude: {longitude}")
    # bot_event_logger.warning

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

#------------------------------------測試時使用gunicorn模擬實際環境------------------------------------#
# 注意: 使用會無法紀錄客製logging, 也許可排除此問題. 正式使用暫時drop.
# def gunicorn(app):
#     from gunicorn.app.base import BaseApplication
#     import gunicorn.glogging
#     import gunicorn.workers.sync
#     class Application(BaseApplication):
#         def load_config(self):
#             self.cfg.set('bind', f"0.0.0.0:{os.environ.get('PORT', 8080)}")
#             workers = 1
#             threads = 8
#             self.cfg.set('workers', workers)
#             self.cfg.set('threads', threads)
#             self.cfg.set('accesslog', "-")
#             access_log_format = '%(t)s %(h)s "%(r)s" %(s)s %(b)s %(D)s "%(a)s"'
#             self.cfg.set('access_log_format', access_log_format)
#         def load(self):
#             return app
#     return Application()

# if __name__ == "__main__":
#     gunicorn(app).run()