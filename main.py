import json
import os
import logging
import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime

from astrbot.api.all import *
from astrbot.api.star import StarTools

# ================= 静态常量 =================
BASE_URL = "https://fanqienovel.com"
# ============================================

@register("fanqie_zerasos_bot", "YourName", "番茄自动更新监控播报", "1.3.0")
class FanqieZerasosPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        # 1. 接收来自 WebUI 的静态配置
        self.config = config or {}
        self._parse_config()
        
        # 2. 初始化持久化数据目录
        self.data_dir = str(StarTools.get_data_dir("fanqie_zerasos_bot"))
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir, exist_ok=True)
            
        self.data_file = os.path.join(self.data_dir, "data.json")
        # chapter_states 变为字典，键为 novel_id，支持多小说共存
        self.data = {"target_groups": [], "chapter_states": {}, "chapter_history": {}}
        self.lock = asyncio.Lock()
        
        self._load_data()
        
        # 3. 启动后台异步定时任务
        self.task = asyncio.create_task(self._background_check_loop())

    def _parse_config(self):
        """解析并清洗配置信息"""
        self.admin_qq = str(self.config.get("admin_qq", "123456789"))
        self.check_interval_min = int(self.config.get("check_interval", 10))
        self.persona_id = str(self.config.get("persona_id", ""))
        
        # 解析 novel_ids，支持中英文逗号，过滤空值
        raw_ids = str(self.config.get("novel_ids", "7656265450392669208"))
        raw_ids = raw_ids.replace("，", ",") 
        self.novel_ids = [n.strip() for n in raw_ids.split(",") if n.strip()]
        
        # 解析各小说剧情概要 novelid:{内容},novelid2:{内容2}
        raw_summaries = str(self.config.get("novel_summaries", ""))
        self.novel_summaries = {}
        if raw_summaries.strip():
            for part in raw_summaries.split(","):
                part = part.strip()
                if not part:
                    continue
                sep = ":" if ":" in part else "："
                if sep in part:
                    nid, summary = part.split(sep, 1)
                    self.novel_summaries[nid.strip()] = summary.strip()
        
        # 解析知识库名称列表（支持前端返回的 list 或字符串格式）
        raw_kb = self.config.get("kb_names", [])
        if isinstance(raw_kb, str):
            self.kb_names = [k.strip() for k in raw_kb.split(",") if k.strip()]
        elif isinstance(raw_kb, list):
            self.kb_names = [str(k).strip() for k in raw_kb if k]
        else:
            self.kb_names = []

    def on_config_update(self, config: dict):
        """WebUI 修改配置后的热重载"""
        self.config = config or {}
        self._parse_config()
        self.persona_id = self.config.get("persona_id", "")
        logging.info(f"[番茄监控] 配置已通过 WebUI 热重载。当前监控间隔: {self.check_interval_min} 分钟。监控 {len(self.novel_ids)} 本小说。")

    def terminate(self):
        if hasattr(self, "task") and self.task:
            self.task.cancel()

    # ========================== 数据持久化管理 ==========================
    
    def _load_data(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        self.data["target_groups"] = loaded.get("target_groups", [])
                        if "chapter_states" in loaded:
                            self.data["chapter_states"] = loaded["chapter_states"]
                        else:
                            self.data["chapter_states"] = {}
                        if "chapter_history" in loaded:
                            self.data["chapter_history"] = loaded["chapter_history"]
            except Exception as e:
                logging.error(f"[番茄监控] 数据文件读取失败: {e}")
        else:
            self._save_data_sync()

    def _save_data_sync(self):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logging.error(f"[番茄监控] 保存数据失败: {e}")

    async def _save_data(self):
        async with self.lock:
            await asyncio.to_thread(self._save_data_sync)

    # ========================== 指令管理模块 ==========================

    @command("fanqie force")
    async def fanqie_force(self, event: AstrMessageEvent):
        sender = str(event.message_obj.sender.user_id)
        logging.info(f"[番茄监控-DEBUG] /fanqie force 被触发! 发送者={sender}, admin_qq={self.admin_qq}")
        if sender != self.admin_qq:
            logging.warning(f"[番茄监控-DEBUG] admin_qq 不匹配! 发送者={sender}, admin_qq={self.admin_qq}")
            return
        yield event.plain_result(f"收到指令，正在强制拉取 {len(self.novel_ids)} 本番茄小说状态...")
        debug_msg, preview_msg = await self.do_check_and_notify(is_debug=True)
        yield event.plain_result(debug_msg)
        if preview_msg:
            yield event.plain_result("【播报内容预览】\n" + preview_msg)

    @command("fanqie get_umo")
    async def fanqie_umo(self, event: AstrMessageEvent):
        if str(event.message_obj.sender.user_id) != self.admin_qq: return
        umo = event.unified_msg_origin
        yield event.plain_result(f"✅ 当前会话的精确底层标识 (UMO) 为：\n{umo}\n\n💡 请将上方标识通过 '/fanqie add [UMO]' 绑定，即可保证 100% 投递成功。")

    @command("fanqie list")
    async def fanqie_list(self, event: AstrMessageEvent):
        if str(event.message_obj.sender.user_id) != self.admin_qq: return
        groups = self.data.get("target_groups", [])
        if not groups:
            yield event.plain_result("当前推送群聊列表为空。\n💡提示：请在目标群聊内发送 /fanqie add 即可快捷绑定。")
            return
        res = "当前正在监控并准备播报的群聊 (UMO) 列表：\n" + "\n".join([f"- {g}" for g in groups])
        yield event.plain_result(res)

    @command("fanqie add")
    async def fanqie_add(self, event: AstrMessageEvent, target_id: str = ""):
        if str(event.message_obj.sender.user_id) != self.admin_qq: return
        target_umo = target_id.strip() or event.unified_msg_origin
        if target_umo not in self.data["target_groups"]:
            self.data["target_groups"].append(target_umo)
            await self._save_data()
            yield event.plain_result(f"✅ 已成功将 '{target_umo}' 加入推送列表。")
        else:
            yield event.plain_result(f"⚠️ '{target_umo}' 已存在于推送列表中。")

    @command("fanqie del")
    async def fanqie_del(self, event: AstrMessageEvent, target_id: str = ""):
        if str(event.message_obj.sender.user_id) != self.admin_qq: return
        target_umo = target_id.strip() or event.unified_msg_origin
        if target_umo in self.data["target_groups"]:
            self.data["target_groups"].remove(target_umo)
            await self._save_data()
            yield event.plain_result(f"✅ 已成功将 '{target_umo}' 移出推送列表。")
        else:
            yield event.plain_result(f"⚠️ '{target_umo}' 不在列表中。")

    @command("fanqie reset")
    async def fanqie_reset(self, event: AstrMessageEvent):
        sender = str(event.message_obj.sender.user_id)
        logging.info(f"[番茄监控-DEBUG] /fanqie reset 被触发! 发送者={sender}, admin_qq={self.admin_qq}")
        if sender != self.admin_qq:
            logging.warning(f"[番茄监控-DEBUG] admin_qq 不匹配! 发送者={sender}, admin_qq={self.admin_qq}")
            return
        self.data["chapter_states"] = {}
        self.data["chapter_history"] = {}
        await self._save_data()
        yield event.plain_result("✅ 已清除本地保存的所有小说章节缓存和正文历史。下一次拉取必定触发更新全群播报。")

    @command("fanqie help")
    async def fanqie_help(self, event: AstrMessageEvent):
        if str(event.message_obj.sender.user_id) != self.admin_qq: return
        help_text = (
            "📖 === 番茄监控插件帮助 ===\n"
            "系统配置：请在 WebUI 中修改多小说ID、管理员权限及检查频率\n"
            "可用指令：\n"
            "1. /fanqie force - 强制检查更新，返回播报预览并全群推送\n"
            "2. /fanqie list - 查看已绑定的推送群聊\n"
            "3. /fanqie add [群号] - 新增推送。强烈建议直接在目标群内发送 '/fanqie add'，系统会自动抓取准确底层标识(UMO)\n"
            "4. /fanqie del [群号] - 移除推送\n"
            "5. /fanqie reset - 清空历史抓取记录\n"
            "6. /fanqie get_umo - 获取当前群底层ID，辅助排错\n"
            "7. /fanqie help - 查看本帮助"
        )
        yield event.plain_result(help_text)

    # ========================== 核心后台逻辑 ==========================

    async def _background_check_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                logging.info(f"[番茄监控] 开始执行后台定时轮询... (共 {len(self.novel_ids)} 本)")
                await self.do_check_and_notify(is_debug=False)
            except Exception as e:
                logging.error(f"[番茄监控] 后台任务异常: {e}")
            # 转换为秒进行休眠，最小间隔为1分钟
            await asyncio.sleep(max(60, self.check_interval_min * 60))

    async def do_check_and_notify(self, is_debug: bool) -> tuple[str, str]:
        """返回 tuple: (Debug信息字符串, 播报预览字符串)"""
        if not self.novel_ids:
            return "[Debug] 监控列表为空，请在 WebUI 配置 novel_ids。", ""

        all_debug_msgs = []
        all_preview_msgs = []

        for novel_id in self.novel_ids:
            target_url = f"{BASE_URL}/page/{novel_id}"
            html_content = await self.get_page_html_async(target_url)
            
            if not html_content: 
                all_debug_msgs.append(f"[Debug] ID:{novel_id} 抓取失败：无法获取网页内容。")
                continue

            novel_title, volume_name, chapter_info, novel_abstract = self.parse_directory_page(html_content)
            if not volume_name or not chapter_info['id']: 
                all_debug_msgs.append(f"[Debug] ID:{novel_id} 抓取失败：未找到章节节点。")
                continue

            # 从多小说字典中读取该小说的状态
            local_state = self.data["chapter_states"].get(novel_id, {})
            local_chapter_id = local_state.get('chapter_id', '')

            if chapter_info['id'] != local_chapter_id:
                # ====== 发现更新 ======
                logging.info(f"⭐ 发现新章节更新！-> 《{novel_title}》 [{volume_name}] {chapter_info['title']}")
                
                result = await self.fetch_chapter_detail_async(chapter_info['full_url'])
                content = result["content"]
                real_update_time = result["update_time"]
                chapter_detail_title = result.get("chapter_title", "")
                word_count = result.get("word_count", "")

                # 更新状态并写入字典
                self.data["chapter_states"][novel_id] = {
                    "novel_title": novel_title,
                    "volume_name": volume_name,
                    "chapter_title": chapter_info['title'],
                    "chapter_id": chapter_info['id'],
                    "last_update_time": real_update_time,
                    "content": content
                }
                
                # 追加到章节历史
                if novel_id not in self.data["chapter_history"]:
                    self.data["chapter_history"][novel_id] = []
                self.data["chapter_history"][novel_id].append({
                    "chapter_title": chapter_info['title'],
                    "chapter_id": chapter_info['id'],
                    "content": content,
                    "update_time": real_update_time,
                    "volume_name": volume_name,
                    "novel_abstract": novel_abstract
                })
                # 只保留最近 20 章
                if len(self.data["chapter_history"][novel_id]) > 20:
                    self.data["chapter_history"][novel_id] = self.data["chapter_history"][novel_id][-20:]
                await self._save_data()
                
                # 构建章节历史上下文（最近5章标题+简短回顾）
                chapter_history = self.data["chapter_history"].get(novel_id, [])[:-1]  # 不包括当前章
                # 生成播报文案
                custom_summary = self.novel_summaries.get(novel_id, "")
                broadcast_msg, ai_debug_lines = await self.generate_broadcast(
                    novel_title, volume_name, chapter_info, real_update_time,
                    content, chapter_detail_title, word_count,
                    novel_abstract, custom_summary, chapter_history
                )
                # 追加 AI debug 信息到已存在的 debug 消息中
                for line in ai_debug_lines:
                    all_debug_msgs.append(line)
                msg_chain = MessageChain().message(broadcast_msg)
                
                # 🔥 推送逻辑：遍历穷举所有可能的前缀 🔥
                success_count = 0
                for target in self.data.get("target_groups", []):
                    sent = False
                    possible_umos = [target]
                    
                    if target.isdigit():
                        possible_umos.extend([
                            f"default:GroupMessage:{target}",
                            f"aiocqhttp-group-{target}", 
                            f"group_{target}", 
                            f"group-{target}", 
                            f"qq_group_{target}"
                        ])
                    
                    for umo in possible_umos:
                        try:
                            await self.context.send_message(umo, msg_chain)
                            sent = True
                            break 
                        except Exception:
                            continue
                            
                    if sent: success_count += 1
                
                all_debug_msgs.append(f"[Debug] 《{novel_title}》发现更新！推送到 {success_count}/{len(self.data.get('target_groups', []))} 个群聊。")
                all_preview_msgs.append(broadcast_msg)
                
            else:
                # ====== 未更新 ======
                if is_debug:
                    msg = (f"【Debug状态】《{novel_title}》 当前最新ID: {chapter_info['id']} | "
                           f"本地记录ID: {local_chapter_id} -> 已是最新。")
                    all_debug_msgs.append(msg)

        return "\n".join(all_debug_msgs), "\n\n".join(all_preview_msgs)

    async def generate_broadcast(self, novel_title, volume_name, chapter_info, update_time,
                                   content="", chapter_detail_title="", word_count="",
                                   novel_abstract="", custom_summary="", chapter_history=None):
        debug = []
        if chapter_history is None:
            chapter_history = []

        # === 构建 prompt ===
        prompt = (f"小说《{novel_title}》更新了。\n"
                  f"更新卷名：{volume_name}\n"
                  f"最新章节：{chapter_info['title']}\n"
                  f"更新时间：{update_time}\n"
                  f"阅读链接：{chapter_info['full_url']}\n")

        if chapter_detail_title:
            prompt += f"章节标题：{chapter_detail_title}\n"
        if word_count:
            prompt += f"本章字数：{word_count}\n"

        # 小说简介
        if novel_abstract:
            prompt += f"\n=== 小说简介 ===\n{novel_abstract}\n=== 简介结束 ===\n"

        # 用户自定义剧情概要
        if custom_summary:
            prompt += f"\n=== 你已知的过去剧情概要（用户自定义）===\n{custom_summary}\n=== 概要结束 ===\n"

        # 过往章节回顾（最近5章标题 + 简短内容片段）
        if chapter_history:
            prompt += "\n=== 过往章节回顾 ===\n"
            for ch in chapter_history[-5:]:
                ch_title = ch.get("chapter_title", "未知章节")
                ch_content = ch.get("content", "")
                snippet = ch_content[:200] if ch_content else "（无正文记录）"
                prompt += f"- {ch_title}：{snippet}\n"
            prompt += "=== 回顾结束 ===\n"

        # 知识库检索
        if self.kb_names and hasattr(self.context, 'kb_manager') and self.context.kb_manager:
            try:
                kb_query = f"{novel_title} {chapter_info['title']} {chapter_detail_title}"
                debug.append(f"[AI-DEBUG] 正在检索知识库: {self.kb_names}, 查询: {kb_query[:50]}...")
                kb_result = await self.context.kb_manager.retrieve(
                    query=kb_query,
                    kb_names=self.kb_names,
                    top_k_fusion=20,
                    top_m_final=5
                )
                if kb_result and kb_result.get("context_text"):
                    kb_text = kb_result["context_text"]
                    prompt += f"\n=== 以下为知识库中检索到的相关内容 ===\n{kb_text}\n=== 知识库内容结束 ===\n"
                    debug.append(f"[AI-DEBUG] ✅ 知识库检索成功，返回 {len(kb_text)} 字符")
                else:
                    debug.append(f"[AI-DEBUG] ⚠️ 知识库检索无结果")
            except Exception as e:
                debug.append(f"[AI-DEBUG] ❌ 知识库检索异常: {type(e).__name__}: {e}")
        elif not self.kb_names:
            debug.append(f"[AI-DEBUG] kb_names 未配置，跳过知识库检索")
        else:
            debug.append(f"[AI-DEBUG] kb_manager 不可用，跳过知识库检索")

        if content:
            snippet = content[:600]
            prompt += (
                f"\n=== 以下为最新章节正文开头（含部分乱码）===\n"
                f"{snippet}\n"
                f"=== 正文结束 ===\n"
                f"注意：正文中含有部分乱码字符，请根据上下文推测其想表达的内容。"
                f"在播报时不要提到乱码问题，正常播报即可。\n"
            )

        prompt += ("\n【要求】：请根据我设置的人格设定进行播报。"
                   "你是一位追更这部小说的读者（以设定人格的口吻），"
                   "阅读最新章节后做出反应——惊讶、吐槽、兴奋、担忧都可以，像读者看完新一章后的自然反应。"
                   "不要以高高在上的角度做总结分析或评价，不要当旁白 narrator。"
                   "直接输出播报语，禁止输出任何 Markdown，禁止自我介绍。"
                   "在输出群播报时，开头包含小说名和章节名以便群友知道更新了哪本。")

        # 预设的播报前缀（标题+链接）
        preset_prefix = f"小说更新啦！《{novel_title}》{chapter_info['title']}\n链接：{chapter_info['full_url']}"

        # ======================== [DEBUG] Provider 探测 ========================
        debug.append(f"[AI-DEBUG] persona_id 配置值: '{self.persona_id}'")
        debug.append(f"[AI-DEBUG] prompt 总长度: {len(prompt)} 字符")
        debug.append(f"[AI-DEBUG] prompt 中是否含乱码正文: {'含乱码正文' if content else '无乱码正文'}")

        all_providers = self.context.get_all_providers()
        debug.append(f"[AI-DEBUG] get_all_providers() 返回类型: {type(all_providers).__name__}")

        provider = None
        if all_providers:
            if isinstance(all_providers, dict):
                debug.append(f"[AI-DEBUG] providers 字典 keys: {list(all_providers.keys())}")
                debug.append(f"[AI-DEBUG] providers values 类型: {[type(v).__name__ for v in all_providers.values()]}")
                provider = next(iter(all_providers.values()), None)
            else:
                debug.append(f"[AI-DEBUG] providers 列表长度: {len(all_providers)}")
                provider = all_providers[0] if len(all_providers) > 0 else None
        else:
            debug.append(f"[AI-DEBUG] get_all_providers() 返回了空")

        if provider:
            debug.append(f"[AI-DEBUG] 选中 provider 类型: {type(provider).__name__}")
            if hasattr(provider, "get_name"):
                try:
                    debug.append(f"[AI-DEBUG] provider 名称: {provider.get_name()}")
                except Exception:
                    pass
        else:
            debug.append(f"[AI-DEBUG] ⚠️ provider 为 None，回退纯文本播报")
            return (f"{preset_prefix}\n━━━━━━━━━━━━━━\n（AI生成失败：无可用Provider）", debug)

        # ======================== [DEBUG] Persona 探测 ========================
        system_prompt = ""
        if self.persona_id:
            try:
                # PersonaManager.get_persona_v3_by_id 是同步方法，返回 Personality (TypedDict)
                persona_obj = self.context.persona_manager.get_persona_v3_by_id(self.persona_id)
                debug.append(f"[AI-DEBUG] get_persona_v3_by_id('{self.persona_id}') 返回: {type(persona_obj).__name__ if persona_obj else 'None'}")
                if persona_obj:
                    sp = persona_obj.get("prompt", "")
                    system_prompt = sp if sp else ""
                    debug.append(f"[AI-DEBUG] 读取到 prompt，长度: {len(system_prompt)} 字符")
                    debug.append(f"[AI-DEBUG] prompt 预览(前200字): {system_prompt[:200]}")
                else:
                    debug.append(f"[AI-DEBUG] ⚠️ persona_obj 为 None，未找到该人格")
            except Exception as e:
                debug.append(f"[AI-DEBUG] ❌ 读取人格异常: {type(e).__name__}: {e}")
        else:
            debug.append(f"[AI-DEBUG] persona_id 为空，不使用自定义人格")

        # ======================== [DEBUG] AI 请求 ========================
        try:
            debug.append(f"[AI-DEBUG] 即将调用 provider.text_chat(prompt=..., system_prompt=...) ...")
            res = await provider.text_chat(prompt=prompt, system_prompt=system_prompt)
            debug.append(f"[AI-DEBUG] provider.text_chat() 调用成功")
            debug.append(f"[AI-DEBUG] response 类型: {type(res).__name__}")
            if res:
                ct = getattr(res, "completion_text", None)
                debug.append(f"[AI-DEBUG] completion_text 存在: {ct is not None}")
                if ct:
                    debug.append(f"[AI-DEBUG] ✅ AI 回复长度: {len(ct)} 字符")
                    debug.append(f"[AI-DEBUG] AI 回复预览(前200字): {ct[:200]}")
                    return (f"{preset_prefix}\n━━━━━━━━━━━━━━\n{ct}", debug)
                else:
                    debug.append(f"[AI-DEBUG] ⚠️ completion_text 为空")
            else:
                debug.append(f"[AI-DEBUG] ⚠️ response 为 None")
        except Exception as e:
            debug.append(f"[AI-DEBUG] ❌ AI 调用异常: {type(e).__name__}: {e}")

        return (f"{preset_prefix}\n━━━━━━━━━━━━━━\n（AI生成失败，使用默认播报）", debug)
            
    # ========================== HTML 解析底层逻辑 ==========================
    
    async def get_page_html_async(self, url):
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=10) as response:
                    response.raise_for_status()
                    return await response.text()
            except Exception as e:
                logging.error(f"[爬虫] 网络请求失败: {e}")
                return None

    def parse_directory_page(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        novel_title, volume_name = "未知小说", "默认卷"
        novel_abstract = ""
        chapter_info = {"title": "", "href": "", "full_url": "", "id": ""}
        
        # 提取小说书名
        title_tag = soup.find('h1') or soup.find('div', class_=lambda c: c and 'info-name' in c)
        if title_tag:
            novel_title = title_tag.get_text(strip=True)
        
        # 提取小说简介
        abstract_div = soup.find('div', class_='page-abstract-content')
        if abstract_div:
            novel_abstract = abstract_div.get_text(strip=True)
        
        dir_cont = soup.find('div', class_='page-directory-content')
        if not dir_cont: return novel_title, None, None, novel_abstract
            
        blocks = dir_cont.find_all('div', recursive=False)
        if not blocks: return novel_title, None, None, novel_abstract
        
        last_block = blocks[-1]
        v_elem = last_block.find('div', class_=lambda c: c and 'volume' in c)
        if v_elem and v_elem.contents:
            volume_name = str(v_elem.contents[0]).strip()
        
        c_cont = last_block.find('div', class_='chapter')
        if c_cont:
            c_items = c_cont.find_all('div', class_='chapter-item')
            if c_items:
                link = c_items[-1].find('a', class_='chapter-item-title')
                if link:
                    chapter_info['title'] = link.get_text(strip=True)
                    chapter_info['href'] = link.get('href')
                    chapter_info['id'] = chapter_info['href'].split('/')[-1] if chapter_info['href'] else ""
                    chapter_info['full_url'] = f"{BASE_URL}{chapter_info['href']}" if chapter_info['href'] else ""
        return novel_title, volume_name, chapter_info, novel_abstract

    async def fetch_chapter_detail_async(self, url):
        html_content = await self.get_page_html_async(url)
        if not html_content:
            return {"content": None, "update_time": "未知时间", "chapter_title": "", "word_count": ""}

        soup = BeautifulSoup(html_content, 'html.parser')
        update_time = "未知时间"
        t_span = soup.find(lambda t: t.name == 'span' and '更新时间' in t.get_text())
        if t_span:
            update_time = t_span.get_text(strip=True).replace('更新时间：', '').replace('更新时间:', '').strip()

        # 提取章节标题
        chapter_title = ""
        title_tag = soup.find('h1', class_='muye-reader-title')
        if title_tag:
            chapter_title = title_tag.get_text(strip=True)

        # 提取本章字数
        word_count = ""
        for span in soup.find_all('span', class_='desc-item'):
            text = span.get_text(strip=True)
            if '字数' in text:
                word_count = text.replace('本章字数：', '').replace('本章字数:', '').strip()

        lines = []
        c_div = soup.find('div', class_=re.compile(r'muye-reader-content'))
        if c_div:
            for p in c_div.find_all('p'):
                if text := p.get_text(strip=True):
                    lines.append(text)

        return {
            "content": "\n\n".join(lines),
            "update_time": update_time,
            "chapter_title": chapter_title,
            "word_count": word_count
        }