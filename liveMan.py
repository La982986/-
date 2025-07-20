#!/usr/bin/python
# coding:utf-8

import codecs
import gzip
import hashlib
import random
import re
import string
import subprocess
import threading
import time
import urllib.parse
from contextlib import contextmanager
from unittest.mock import patch

import requests
import websocket
import json
from py_mini_racer import MiniRacer
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
from protobuf.douyin import *


@contextmanager
def patched_popen_encoding(encoding='utf-8'):
    original_popen_init = subprocess.Popen.__init__

    def new_popen_init(self, *args, **kwargs):
        kwargs['encoding'] = encoding
        original_popen_init(self, *args, **kwargs)

    with patch.object(subprocess.Popen, '__init__', new_popen_init):
        yield


def generateSignature(wss, script_file='sign.js'):
    """
    出现gbk编码问题则修改 python模块subprocess.py的源码中Popen类的__init__函数参数encoding值为 "utf-8"
    """
    params = ("live_id,aid,version_code,webcast_sdk_version,"
              "room_id,sub_room_id,sub_channel_id,did_rule,"
              "user_unique_id,device_platform,device_type,ac,"
              "identity").split(',')
    wss_params = urllib.parse.urlparse(wss).query.split('&')
    wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ','.join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()

    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()

    ctx = MiniRacer()
    ctx.eval(script)

    try:
        signature = ctx.call("get_sign", md5_param)
        return signature
    except Exception as e:
        print(e)

    # 以下代码对应js脚本为sign_v0.js
    # context = execjs.compile(script)
    # with patched_popen_encoding(encoding='utf-8'):
    #     ret = context.call('getSign', {'X-MS-STUB': md5_param})
    # return ret.get('X-Bogus')


def generateMsToken(length=107):
    """
    产生请求头部cookie中的msToken字段，其实为随机的107位字符
    :param length:字符位数
    :return:msToken
    """
    random_str = ''
    base_str = string.ascii_letters + string.digits + '=_'
    _len = len(base_str) - 1
    for _ in range(length):
        random_str += base_str[random.randint(0, _len)]
    return random_str


class DouyinLiveWebFetcher:

    def __init__(self, live_id, log_callback=None):
        """
        直播间弹幕抓取对象
        :param live_id: 直播间的直播id，打开直播间web首页的链接如：https://live.douyin.com/261378947940  ，
                        其中的261378947940即是live_id
        :param log_callback: 日志回调函数
        """
        self.__ttwid = None
        self.__room_id = None
        self.live_id = live_id
        self.live_url = "https://live.douyin.com/"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " \
                          "Chrome/120.0.0.0 Safari/537.36"
        self.log_callback = log_callback
        self.ws = None
        self.heartbeat_thread = None
        self.running = False

    def log(self, log_type, message):
        """记录日志"""
        if self.log_callback:
            self.log_callback(log_type, message)
        else:
            print(f"[{log_type}] {message}")

    def start(self):
        self.running = True
        self._connectWebSocket()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=1.0)

    @property
    def ttwid(self):
        """
        产生请求头部cookie中的ttwid字段，访问抖音网页版直播间首页可以获取到响应cookie中的ttwid
        :return: ttwid
        """
        if self.__ttwid:
            return self.__ttwid
        headers = {
            "User-Agent": self.user_agent,
        }
        try:
            response = requests.get(self.live_url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播URL错误: {err}")
        else:
            self.__ttwid = response.cookies.get('ttwid')
            return self.__ttwid

    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        url = self.live_url + self.live_id
        headers = {
            "User-Agent": self.user_agent,
            "cookie": f"ttwid={self.ttwid}&msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4",
        }
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            self.log("ERROR", f"请求直播间URL错误: {err}")
        else:
            match = re.search(r'roomId\\":\\"(\d+)\\"', response.text)
            if match is None or len(match.groups()) < 1:
                self.log("ERROR", "未找到匹配的roomId")

            self.__room_id = match.group(1)
            return self.__room_id

    def get_room_status(self):
        """
        获取直播间开播状态:
        room_status: 2 直播已结束
        room_status: 0 直播进行中
        """
        url = ('https://live.douyin.com/webcast/room/web/enter/?aid=6383'
               '&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=web_live'
               '&cookie_enabled=true&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32'
               '&browser_name=Edge&browser_version=133.0.0.0'
               f'&web_rid={self.live_id}'
               f'&room_id_str={self.room_id}'
               '&enter_source=&is_need_double_stream=false&insert_task_id=&live_reason='
               '&msToken=&a_bogus=')
        try:
            resp = requests.get(url, headers={
                'User-Agent': self.user_agent,
                'Cookie': f'ttwid={self.ttwid};'
            })
            resp.raise_for_status()
            data = resp.json().get('data')
            if data:
                room_status = data.get('room_status')
                user = data.get('user')
                user_id = user.get('id_str')
                nickname = user.get('nickname')
                status = '正在直播' if room_status == 0 else '已结束'
                self.log("STATUS", f"【{nickname}】[{user_id}]直播间：{status}.")
                return True, status, nickname, user_id
            else:
                self.log("ERROR", "获取直播间状态失败，返回数据为空")
                return False, "未知", "未知", "未知"
        except Exception as e:
            self.log("ERROR", f"获取直播间状态时出错: {str(e)}")
            return False, "错误", "未知", "未知"

    def get_audience_ranklist(self, anchor_id):
        """
        获取直播间观众用户数据
        """
        # 构建URL
        url = f"https://live.douyin.com/webcast/ranklist/audience/?aid=6383&app_name=douyin_web&webcast_sdk_version=2450&room_id={self.room_id}&anchor_id={anchor_id}&rank_type=30&a_bogus="
        url2 = f"https://live.douyin.com/webcast/ranklist/audience/?aid=6383&app_name=douyin_web&webcast_sdk_version=2450&room_id={self.room_id}&anchor_id={anchor_id}&rank_type=30&a_bogus="

        headers = {
            'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0",
        }

        self.log("RANK", f"获取观众用户数据数据中.....")

        try:
            # 先尝试普通用户路线
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = json.loads(response.text)

            # 检查响应中是否包含数据
            if 'data' not in data or 'ranks' not in data['data']:
                # 如果普通用户路线失败，尝试VIP路线
                self.log("RANK", "普通用户路线未获取到数据，尝试VIP路线...")
                response = requests.get(url2, headers=headers)
                response.raise_for_status()
                data = json.loads(response.text)

                if 'data' not in data or 'ranks' not in data['data']:
                    self.log("ERROR", "VIP路线也未获取到排名数据，请检查输入的房间ID和主播ID是否正确")
                    return []

            ranks = data['data']['ranks']
            account_list = []
            for rank in ranks:
                # 确保用户信息存在
                if 'user' in rank and 'id' in rank['user']:
                    user = rank['user']
                    account_info = {
                        'id': user.get('id', '未知'),
                        'nickname': user.get('nickname', '未知昵称'),
                        'display_id': user.get('display_id', '')
                    }
                    account_list.append(account_info)
                else:
                    self.log("WARN", f"警告：第{rank.get('rank', '未知')}位用户数据缺失")

            self.log("RANK", f"成功获取到 {len(account_list)} 个账号信息")
            return account_list
        except Exception as e:
            self.log("ERROR", f"获取观众用户数据时出错: {str(e)}")
            return []

    def _connectWebSocket(self):
        """
        连接抖音直播间websocket服务器，请求直播间数据
        """
        if not self.room_id:
            self.log("ERROR", "无法获取room_id，无法连接WebSocket")
            return

        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
               f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:7319483754668557238"
               f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
               f"wrds_v:7392094459690748497"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id=7319483754668557238&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")

        signature = generateSignature(wss)
        wss += f"&signature={signature}"

        headers = {
            "cookie": f"ttwid={self.ttwid}",
            'user-agent': self.user_agent,
        }

        self.log("WEBSOCKET", f"正在连接WebSocket: {wss[:100]}...")

        try:
            self.ws = websocket.WebSocketApp(wss,
                                             header=headers,
                                             on_open=self._wsOnOpen,
                                             on_message=self._wsOnMessage,
                                             on_error=self._wsOnError,
                                             on_close=self._wsOnClose)
            self.ws.run_forever()
        except Exception as e:
            self.log("ERROR", f"WebSocket连接错误: {str(e)}")
            self.stop()

    def _sendHeartbeat(self):
        """
        发送心跳包
        """
        while self.running:
            try:
                if self.ws and self.ws.sock and self.ws.sock.connected:
                    heartbeat = PushFrame(payload_type='hb').SerializeToString()
                    self.ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
                    self.log("HEARTBEAT", "发送心跳包...")
                else:
                    self.log("WARN", "WebSocket未连接，停止发送心跳")
                    break
            except Exception as e:
                self.log("ERROR", f"发送心跳包时出错: {str(e)}")
                break
            else:
                time.sleep(5)

    def _wsOnOpen(self, ws):
        """
        连接建立成功
        """
        self.log("WEBSOCKET", "WebSocket连接成功.")
        self.heartbeat_thread = threading.Thread(target=self._sendHeartbeat)
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()

    def _wsOnMessage(self, ws, message):
        """
        接收到数据
        :param ws: websocket实例
        :param message: 数据
        """

        # 根据proto结构体解析对象
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))

        # 返回直播间服务器链接存活确认消息，便于持续获取数据
        if response.need_ack:
            try:
                ack = PushFrame(log_id=package.log_id,
                                payload_type='ack',
                                payload=response.internal_ext.encode('utf-8')
                                ).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                self.log("ERROR", f"发送ACK时出错: {str(e)}")

        # 根据消息类别解析消息体
        for msg in response.messages_list:
            method = msg.method
            try:
                {
                    'WebcastChatMessage': self._parseChatMsg,  # 聊天消息
                    'WebcastGiftMessage': self._parseGiftMsg,  # 礼物消息
                    'WebcastLikeMessage': self._parseLikeMsg,  # 点赞消息
                    'WebcastMemberMessage': self._parseMemberMsg,  # 进入直播间消息
                    'WebcastSocialMessage': self._parseSocialMsg,  # 关注消息
                    'WebcastRoomUserSeqMessage': self._parseRoomUserSeqMsg,  # 直播间统计
                    'WebcastFansclubMessage': self._parseFansclubMsg,  # 粉丝团消息
                    'WebcastControlMessage': self._parseControlMsg,  # 直播间状态消息
                    'WebcastEmojiChatMessage': self._parseEmojiChatMsg,  # 聊天表情包消息
                    'WebcastRoomStatsMessage': self._parseRoomStatsMsg,  # 直播间统计信息
                    'WebcastRoomMessage': self._parseRoomMsg,  # 直播间信息
                    'WebcastRoomRankMessage': self._parseRankMsg,  # 直播间用户数据信息
                    'WebcastRoomStreamAdaptationMessage': self._parseRoomStreamAdaptationMsg,  # 直播间流配置
                }.get(method)(msg.payload)
            except Exception as e:
                self.log("ERROR", f"尝试解析消息可能出错: {str(e)}")

    def _wsOnError(self, ws, error):
        self.log("ERROR", f"WebSocket错误: {str(error)}")

    def _wsOnClose(self, ws, *args):
        self.log("WEBSOCKET", "WebSocket连接已关闭.")
        self.running = False

    def _parseChatMsg(self, payload):
        """聊天消息"""
        message = ChatMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        content = message.content
        self.log("CHAT", f"[{user_id}]{user_name}: {content}")

    def _parseGiftMsg(self, payload):
        """礼物消息"""
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        gift_name = message.gift.name
        gift_cnt = message.combo_count
        self.log("GIFT", f"{user_name} 送出了 {gift_name}x{gift_cnt}")

    def _parseLikeMsg(self, payload):
        '''点赞消息'''
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        count = message.count
        self.log("LIKE", f"{user_name} 点了{count}个赞")

    def _parseMemberMsg(self, payload):
        '''进入直播间消息'''
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gender = ["女", "男"][message.user.gender]
        self.log("ENTER", f"[{user_id}][{gender}]{user_name} 进入了直播间")

    def _parseSocialMsg(self, payload):
        '''关注消息'''
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        self.log("FOLLOW", f"[{user_id}]{user_name} 关注了主播")

    def _parseRoomUserSeqMsg(self, payload):
        '''直播间统计'''
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        self.log("STATS", f"当前观看人数: {current}, 累计观看人数: {total}")

    def _parseFansclubMsg(self, payload):
        '''粉丝团消息'''
        message = FansclubMessage().parse(payload)
        content = message.content
        self.log("FANSCLUB", content)

    def _parseEmojiChatMsg(self, payload):
        '''聊天表情包消息'''
        message = EmojiChatMessage().parse(payload)
        emoji_id = message.emoji_id
        user = message.user
        common = message.common
        default_content = message.default_content
        self.log("EMOJI", f"表情包ID: {emoji_id}, 用户: {user}, 内容: {default_content}")

    def _parseRoomMsg(self, payload):
        message = RoomMessage().parse(payload)
        common = message.common
        room_id = common.room_id
        self.log("ROOM", f"直播间ID: {room_id}")

    def _parseRoomStatsMsg(self, payload):
        message = RoomStatsMessage().parse(payload)
        display_long = message.display_long
        self.log("STATS", display_long)

    def _parseRankMsg(self, payload):
        message = RoomRankMessage().parse(payload)
        ranks_list = message.ranks_list
        self.log("RANK", f"用户数据: {ranks_list}")

    def _parseControlMsg(self, payload):
        '''直播间状态消息'''
        message = ControlMessage().parse(payload)
        if message.status == 3:
            self.log("STATUS", "直播间已结束")
            self.stop()

    def _parseRoomStreamAdaptationMsg(self, payload):
        message = RoomStreamAdaptationMessage().parse(payload)
        adaptationType = message.adaptation_type
        self.log('ADAPTATION', f'直播间adaptation: {adaptationType}')


class DouyinLiveApp:
    def __init__(self, root):
        self.root = root
        self.root.title("抖音直播间监控工具")
        self.root.geometry("1200x800")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 创建日志类型字典
        self.log_types = {
            "CHAT": "聊天消息",
            "GIFT": "礼物消息",
            "LIKE": "点赞消息",
            "ENTER": "进场消息",
            "FOLLOW": "关注消息",
            "STATS": "统计信息",
            "FANSCLUB": "粉丝团消息",
            "EMOJI": "表情消息",
            "ROOM": "房间信息",
            "RANK": "用户数据信息",
            "ADAPTATION": "流配置",
            "STATUS": "房间状态",
            "WEBSOCKET": "连接状态",
            "HEARTBEAT": "心跳检测",
            "ERROR": "错误信息",
            "WARN": "警告信息"
        }

        # 创建UI
        self.create_widgets()

        # 直播监控器实例
        self.fetcher = None
        self.live_id = ""
        self.anchor_id = ""

    def create_widgets(self):
        # 创建顶部控制面板
        control_frame = ttk.Frame(self.root, padding="10")
        control_frame.grid(row=0, column=0, columnspan=3, sticky="ew")

        # 直播间ID输入
        ttk.Label(control_frame, text="直播间ID:").grid(row=0, column=0, padx=5, sticky="w")
        self.live_id_entry = ttk.Entry(control_frame, width=30)
        self.live_id_entry.grid(row=0, column=1, padx=5)

        # 主播ID输入
        ttk.Label(control_frame, text="主播ID:").grid(row=0, column=2, padx=5, sticky="w")
        self.anchor_id_entry = ttk.Entry(control_frame, width=30)
        self.anchor_id_entry.grid(row=0, column=3, padx=5)

        # 按钮区域
        button_frame = ttk.Frame(control_frame)
        button_frame.grid(row=0, column=4, padx=10)

        ttk.Button(button_frame, text="获取直播间状态", command=self.get_status).grid(row=0, column=0, padx=5)
        ttk.Button(button_frame, text="获取用户数据", command=self.get_ranklist).grid(row=0, column=1, padx=5)
        ttk.Button(button_frame, text="开始直播间数据监控", command=self.start_monitor).grid(row=0, column=2, padx=5)
        ttk.Button(button_frame, text="停止监控", command=self.stop_monitor).grid(row=0, column=3, padx=5)
        ttk.Button(button_frame, text="清空日志", command=self.clear_logs).grid(row=0, column=4, padx=5)

        # 创建4x3网格的日志框
        self.log_frames = {}
        self.log_texts = {}

        # 定义日志框的位置和类型
        log_positions = [
            (1, 0, "CHAT"),  # 聊天消息
            (1, 1, "GIFT"),  # 礼物消息
            (1, 2, "ENTER"),  # 进场消息
            (2, 0, "LIKE"),  # 点赞消息
            (2, 1, "FOLLOW"),  # 关注消息
            (2, 2, "FANSCLUB"),  # 粉丝团消息
            (3, 0, "STATS"),  # 统计信息
            (3, 1, "STATUS"),  # 房间状态
            (3, 2, "RANK"),  # 用户数据信息
            (4, 0, "ROOM"),  # 房间信息
            (4, 1, "ADAPTATION"),  # 流配置
            (4, 2, "ERROR")  # 错误信息
        ]

        for row, col, log_type in log_positions:
            frame = ttk.LabelFrame(self.root, text=self.log_types[log_type])
            frame.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")

            # 创建带滚动条的文本框
            text_area = scrolledtext.ScrolledText(
                frame,
                wrap=tk.WORD,
                width=40,
                height=10,
                state='disabled'
            )
            text_area.pack(fill="both", expand=True)

            self.log_frames[log_type] = frame
            self.log_texts[log_type] = text_area

        # 配置网格行列权重
        for i in range(1, 5):
            self.root.rowconfigure(i, weight=1)
        for i in range(3):
            self.root.columnconfigure(i, weight=1)

    def log_message(self, log_type, message):
        """记录日志到对应的文本框"""
        if log_type in self.log_texts:
            text_area = self.log_texts[log_type]
            text_area.config(state='normal')
            text_area.insert(tk.END, message + "\n")
            text_area.see(tk.END)  # 滚动到底部
            text_area.config(state='disabled')

    def get_status(self):
        """获取直播间状态"""
        self.live_id = self.live_id_entry.get().strip()
        if not self.live_id:
            messagebox.showerror("错误", "请输入直播间ID")
            return

        if not self.fetcher or self.fetcher.live_id != self.live_id:
            self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_message)

        success, status, nickname, user_id = self.fetcher.get_room_status()
        if success:
            messagebox.showinfo("直播间状态", f"主播: {nickname}\nID: {user_id}\n状态: {status}")
        else:
            messagebox.showerror("错误", "无法获取直播间状态")

    def get_ranklist(self):
        """获取观众用户数据"""
        self.live_id = self.live_id_entry.get().strip()
        self.anchor_id = self.anchor_id_entry.get().strip()

        if not self.live_id:
            messagebox.showerror("错误", "请输入直播间ID")
            return

        if not self.anchor_id:
            messagebox.showerror("错误", "请输入主播ID")
            return

        if not self.fetcher or self.fetcher.live_id != self.live_id:
            self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_message)

        accounts = self.fetcher.get_audience_ranklist(self.anchor_id)

        # 显示用户数据结果
        if accounts:
            rank_window = tk.Toplevel(self.root)
            rank_window.title("直播间观众用户数据")
            rank_window.geometry("600x400")

            # 创建树形视图
            tree = ttk.Treeview(rank_window, columns=("ID", "昵称", "抖音号"), show="headings")
            tree.heading("ID", text="ID")
            tree.heading("昵称", text="昵称")
            tree.heading("抖音号", text="抖音号")

            tree.column("ID", width=100)
            tree.column("昵称", width=200)
            tree.column("抖音号", width=200)

            # 添加滚动条
            scrollbar = ttk.Scrollbar(rank_window, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scrollbar.set)

            scrollbar.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)

            # 添加数据
            for i, account in enumerate(accounts, 1):
                tree.insert("", "end", values=(account['id'], account['nickname'], account['display_id']))
        else:
            messagebox.showinfo("提示", "未获取到观众用户数据数据")

    def start_monitor(self):
        """开始监控直播间"""
        self.live_id = self.live_id_entry.get().strip()
        if not self.live_id:
            messagebox.showerror("错误", "请输入直播间ID")
            return

        # 检查是否已经有监控器在运行
        if self.fetcher and self.fetcher.running:
            messagebox.showinfo("提示", "监控已在运行中")
            return

        # 创建或更新监控器
        if not self.fetcher or self.fetcher.live_id != self.live_id:
            self.fetcher = DouyinLiveWebFetcher(self.live_id, self.log_message)

        # 先获取房间状态
        success, status, nickname, user_id = self.fetcher.get_room_status()
        if not success:
            messagebox.showerror("错误", "无法获取直播间状态，监控无法启动")
            return

        if status != "正在直播":
            if not messagebox.askyesno("确认", "直播间当前未开播，是否继续监控？"):
                return

        # 启动监控线程
        monitor_thread = threading.Thread(target=self.fetcher.start)
        monitor_thread.daemon = True
        monitor_thread.start()

        self.log_message("STATUS", "直播间监控已启动...")

    def stop_monitor(self):
        """停止监控直播间"""
        if self.fetcher:
            self.fetcher.stop()
            self.log_message("STATUS", "直播间监控已停止")

    def clear_logs(self):
        """清空所有日志"""
        for text_area in self.log_texts.values():
            text_area.config(state='normal')
            text_area.delete(1.0, tk.END)
            text_area.config(state='disabled')
        self.log_message("STATUS", "所有日志已清空")

    def on_closing(self):
        """关闭窗口时的处理"""
        if self.fetcher and self.fetcher.running:
            if messagebox.askokcancel("退出", "监控正在运行，确定要退出吗？"):
                self.fetcher.stop()
                self.root.destroy()
        else:
            self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    app = DouyinLiveApp(root)
    root.mainloop()