"""一次性补丁：P1.6E-R1 非阻塞审核工作流（正常事件 0 弹窗）（用后即删）"""
import io

p = 'desktop.py'
src = io.open(p, encoding='utf-8').read()
orig = src

# ---- 1) 空文件夹：弹窗 → 状态栏 ----
old = """            self._say('这个文件夹里没有 .mcap / .mp4 视频文件')
            QMessageBox.information(self, APP_NAME,
                                    '所选文件夹里没有找到 .mcap 录像或 .mp4 视频。')
            return"""
new = """            # P1.6E-R1：正常事件 0 弹窗，只走状态栏
            self._say('这个文件夹里没有找到 .mcap 录像或 .mp4 视频', 8000)
            return"""
assert old in src, 'empty folder dialog not found'
src = src.replace(old, new, 1)

# ---- 2) 缺 opencv：critical → 状态栏（可恢复错误，不阻塞） ----
old = """        if not HAS_CV:
            QMessageBox.critical(self, APP_NAME,
                                 '缺少 opencv-python，无法解码视频。\\n'
                                 '请运行：pip install opencv-python')"""
new = """        if not HAS_CV:
            # P1.6E-R1：可恢复错误不弹窗，改状态栏 + 行内提示
            self._say('缺少 opencv-python，无法解码视频；请运行：pip install opencv-python',
                      15000)
            if sid:
                self.qm.mark_error(sid, '缺少 opencv-python') if hasattr(
                    self.qm, 'mark_error') else None
            return"""
assert old in src, 'opencv dialog not found'
src = src.replace(old, new, 1)

# ---- 3) 文件夹全部看完：询问弹窗 → 状态栏（不清空，用户自己决定） ----
old = """        self._prompted_folder = self.folder
        path = self._generate_report()
        summary = MK.summarize(self.qm.summary_items(), self.folder)
        if self._confirm_folder_complete(summary, path):
            self.clear_queues_and_cache(ask=False, then_choose=True)
        else:
            self._say('已保留当前队列与缓存；随时可点左侧「清空队列与缓存」', 15000)

    def _confirm_folder_complete(self, summary, path):
        \"\"\"弹窗询问；返回 True 表示「清空并选下一个文件夹」。测试可覆盖本方法。\"\"\"
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Question)
        box.setText('这个文件夹的 %d 个视频已全部看完。' % summary['count'])
        ask = ('是否清空已看完队列（本地视频不产生缓存），并选择下一个文件夹？'
               if self.folder_mode() == 'mp4' else
               '是否清空已看完队列与所有缓存，并选择下一个文件夹？')
        box.setInformativeText(
            '定位合格率：%.2f%%（总时长 %.1f 秒 / 不合格 %.1f 秒）\\n'
            '报告已保存到：%s\\n\\n%s'
            % (summary['rate'], summary['total'], summary['bad'],
               path or '（生成失败，请检查文件夹权限）', ask))
        yes = box.addButton('是，清空并选择下一个文件夹', QMessageBox.AcceptRole)
        box.addButton('否，先不动', QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes"""
new = """        self._prompted_folder = self.folder
        path = self._generate_report()
        summary = MK.summarize(self.qm.summary_items(), self.folder)
        # P1.6E-R1：正常事件 0 弹窗 —— 只在状态栏报告，清空由用户主动点按钮（那里保留确认）
        self._say('本文件夹 %d 个视频已全部看完 · 定位合格率 %.2f%% · 报告：%s；'
                  '需要清空时点左侧「清空队列与缓存」'
                  % (summary['count'], summary['rate'],
                     os.path.basename(path) if path else '生成失败'), 20000)

    def _confirm_folder_complete(self, summary, path):
        \"\"\"P1.6E-R1：文件夹全部看完**不再弹窗**（正常事件 0 modal）。

        返回 False = 不自动清空（用户需要时自己点「清空队列与缓存」）。
        测试可覆盖本方法以确认无 QMessageBox.exec 调用。
        \"\"\"
        return False"""
assert old in src, 'folder complete dialog not found'
src = src.replace(old, new, 1)

# ---- 4) 导出/保存失败：warning → 状态栏 ----
old = """        except OSError:
            QMessageBox.warning(self, APP_NAME, '保存失败，请换一个路径试试。')"""
new = """        except OSError:
            # P1.6E-R1：可恢复错误不弹窗
            self._say('保存失败，请换一个路径试试', 8000)"""
assert old in src, 'save warning not found'
src = src.replace(old, new, 1)

old = """        except Exception as e:
            QMessageBox.warning(self, APP_NAME, '导出失败：%s' % e)"""
new = """        except Exception as e:
            # P1.6E-R1：可恢复错误不弹窗
            self._say('导出失败：%s' % e, 10000)"""
assert old in src, 'export warning not found'
src = src.replace(old, new, 1)

assert src != orig
io.open(p, 'w', encoding='utf-8').write(src)
print('R1 patched: 5 处弹窗改为状态栏（保留清空缓存/清空标注/严重错误弹窗）')
