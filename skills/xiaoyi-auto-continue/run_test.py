#!/usr/bin/env python3
"""
run_test.py - 测试集编排器

整合 setup_device.py（文件发送）和模块化的任务执行逻辑，
支持断点续传。直接监控远程日志判定任务完成状态。
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 导入本地模块
from scripts.hdc_client import snapshot, list_remote_logs, changed_logs, HdcCommandLogger, set_hdc_logger
from scripts.log_monitor import wait_for_task_done, today_id, TaskTimeoutError, read_remote_stop_candidates, has_stop_reason_stop
from scripts.task_executor import start_prompt, force_stop, read_prompt_text, pull_log, pull_outputs, extract_stop_content, count_stop_events, _ACCESSIBLE_OUTPUT_ROOTS, save_prompt_text
from scripts.setup_device import setup_single_case, get_all_cases
from scripts.case_manager import is_case_completed, mark_case_completed, mark_case_failed, mark_case_interrupted, load_config
from scripts.get_dialog_page_id import get_latest_dialog_page_id, fetch_history_list


def run_single_case(case_id: str, config: dict, run_dir: str, verbose: bool = False, skip_setup: bool = False, clean: bool = False) -> bool:
    """运行单个case的完整流程（直接监控远程日志判定完成状态）"""
    prompts_dir = config.get('prompts_dir', 'prompts')
    prompt_file = os.path.join(prompts_dir, f"{case_id}.txt")
    target = config.get('hdc_target')
    before = {}  # 日志快照，用于中断时拉取日志

    if not os.path.exists(prompt_file):
        print(f"Error: prompt file not found: {prompt_file}", file=sys.stderr)
        return False

    # 检查是否已完成
    if is_case_completed(case_id, run_dir):
        print(f"Case {case_id} already completed (skipping)")
        return True

    print(f"\n{'='*60}")
    print(f"Running case: {case_id}")
    print(f"{'='*60}")

    # ============================================
    # Step 1: 清理输出目录 + 发送文件
    # ============================================
    if not skip_setup:
        # 始终清理 Desktop/Download/Documents
        from scripts.hdc_client import remote_shell, shell_quote, HdcError

        print(f"[1/3] Cleaning output directories for {case_id}...")
        for dir_path in _ACCESSIBLE_OUTPUT_ROOTS:
            rm_cmd = f'rm -rf {dir_path}/*'
            print(f"[HDC] hdc shell sh -c {rm_cmd}")
            try:
                out = remote_shell(rm_cmd, target=target, timeout=30, verbose=verbose)
                if out.strip():
                    print(f"[HDC] {out.strip()}")
                # 验证是否清理成功
                ls_out = remote_shell(f'ls {dir_path}/', target=target, timeout=30, verbose=verbose)
                if ls_out.strip():
                    print(f"[{case_id}] WARNING: {dir_path} still has files: {ls_out.strip()}")
                else:
                    print(f"[{case_id}] Cleaned {dir_path}")
            except HdcError as e:
                print(f"[{case_id}] Warning: failed to clean {dir_path}: {e}")
            except Exception as e:
                print(f"[{case_id}] Warning: failed to clean {dir_path}: {e}")

        # 清理远程目录后，删除本地 setup_done.json 标记，确保重新发送文件
        setup_done_file = os.path.join(run_dir, case_id, 'setup_done.json')
        if os.path.exists(setup_done_file):
            os.remove(setup_done_file)
            print(f"[{case_id}] Removed setup_done.json marker")

        print(f"[2/3] Setting up files for {case_id}...")
        setup_ok = setup_single_case(case_id, config, run_dir=run_dir, verbose=verbose, clean=False)
        if not setup_ok:
            print(f"Error: setup failed for {case_id}", file=sys.stderr)
            mark_case_failed(case_id, run_dir, "setup failed")
            return False

        # 等待文件同步
        time.sleep(2)
    else:
        print(f"[1/3] Skipping setup (--skip-setup)")

    # Step 2: 运行测试（直接监控远程日志）
    print(f"[3/3] Running test for {case_id}...")

    timeout = config.get('xiaoyi_timeout', 1200)  # 默认20分钟超时
    poll_seconds = 3.0
    tail_lines = 300

    try:
        # 读取 prompt 内容
        query_text = read_prompt_text(prompt_file)

        # 保存推送给小艺的 query 文本到 case 目录，便于事后回查
        save_prompt_text(query_text, case_id=case_id, run_dir=run_dir, tag="prompt")

        # 获取执行前日志快照
        current_date_id = today_id()
        before = snapshot(list_remote_logs(
            target=target,
            user_id=None,
            date_id=current_date_id,
            verbose=verbose,
        ))

        # 启动任务
        print(f"[{case_id}] Starting prompt...")
        start_prompt(query_text, target=target, verbose=verbose)

        # 等待任务完成
        print(f"[{case_id}] Waiting for task to complete...")
        done_log = wait_for_task_done(
            item_id=case_id,
            before=before,
            target=target,
            user_id=None,
            initial_date_id=current_date_id,
            dynamic_date=False,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout,
            tail_lines=tail_lines,
            verbose=verbose,
        )

        print(f"[{case_id}] Task completed, log: {done_log.path}")

        # 等待日志稳定
        time.sleep(1.5)

        # 拉取日志
        print(f"[{case_id}] [Phase 1/4] Pulling log...")
        local_log = pull_log(
            done_log,
            case_id=case_id,
            run_dir=run_dir,
            target=target,
            verbose=verbose,
        )
        print(f"[{case_id}] Log pulled to: {local_log}")

        # 任务完成后获取最新的 dialogPageId
        # 注意：必须放在任务完成后取，否则 history_list.json 可能还没刷新出当前新对话，
        # 会取到上一个对话的 dialogPageId，导致后续 --continue 续话失败
        print(f"[{case_id}] Fetching latest dialogPageId...")
        dialog_page_id = get_latest_dialog_page_id(target=target)
        if dialog_page_id:
            print(f"[{case_id}] dialogPageId: {dialog_page_id}")
        else:
            print(f"[{case_id}] Warning: no dialogPageId found")

        # 更新 meta.json，添加 dialog_page_id
        meta_file = Path(run_dir) / case_id / f"{case_id}.meta.json"
        if meta_file.exists() and dialog_page_id:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
            meta['dialog_page_id'] = dialog_page_id
            meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"[{case_id}] dialog_page_id updated in meta.json")

        # 解析日志，提取 stop content 并输出
        print(f"[{case_id}] [Phase 2/4] Extracting stop content...")
        extract_stop_content(local_log, case_id, run_dir)

        # Force stop 小艺
        print(f"[{case_id}] [Phase 3/4] Force stopping...")
        force_stop(target=target, verbose=verbose)
        print(f"[{case_id}] Force stopped com.huawei.hmos.vassistant")

        # 拉取 Desktop/Downloads/Documents 下的输出文件
        print(f"[{case_id}] [Phase 4/4] Pulling output files...")
        pulled = pull_outputs(case_id, run_dir, target, verbose=verbose)
        print(f"[{case_id}] Pulled {len(pulled)} output files")

        # 标记完成
        mark_case_completed(case_id, run_dir)
        print(f"Case {case_id} completed successfully")
        return True

    except TaskTimeoutError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        print(f"[{case_id}] Task timeout, pulling available data...")

        # Force stop 小艺
        print(f"[{case_id}] [Phase 3/4] Force stopping...")
        force_stop(target=target, verbose=verbose)
        print(f"[{case_id}] Force stopped com.huawei.hmos.vassistant")

        # 尝试拉取已有日志（如果能获取到active_log）
        if exc.active_log is not None:
            try:
                print(f"[{case_id}] [Phase 1/4] Pulling log...")
                local_log = pull_log(
                    exc.active_log,
                    case_id=case_id,
                    run_dir=run_dir,
                    target=target,
                    verbose=verbose,
                )
                print(f"[{case_id}] Log pulled to: {local_log}")

                # 尝试解析日志（即使超时也可能已有部分结果）
                print(f"[{case_id}] [Phase 2/4] Extracting stop content...")
                extract_stop_content(local_log, case_id, run_dir)
            except Exception as e:
                print(f"[{case_id}] Failed to pull log: {e}", file=sys.stderr)

        # 拉取 Desktop/Downloads/Documents 下的输出文件
        print(f"[{case_id}] [Phase 4/4] Pulling output files...")
        pulled = pull_outputs(case_id, run_dir, target, verbose=verbose)
        print(f"[{case_id}] Pulled {len(pulled)} output files")

        mark_case_failed(case_id, run_dir, str(exc))
        print(f"[{case_id}] Marked as failed (timeout)")
        return False

    except KeyboardInterrupt:
        print(f"\n[{case_id}] 手动中断，正在拉取数据...")

        # Force stop 小艺
        try:
            force_stop(target=target, verbose=verbose)
            print(f"[{case_id}] Force stopped com.huawei.hmos.vassistant")
        except:
            pass

        # 尝试拉取当前活跃日志
        try:
            current_logs = list_remote_logs(target=target, user_id=None, date_id=today_id(), verbose=verbose)
            changed_list = changed_logs(before, current_logs) if 'before' in dir() and before else current_logs
            if changed_list:
                active = changed_list[0]
                local_log = pull_log(active, case_id=case_id, run_dir=run_dir, target=target, verbose=verbose)
                print(f"[{case_id}] Log pulled to: {local_log}")
            else:
                print(f"[{case_id}] No active log found to pull")
        except Exception as e:
            print(f"[{case_id}] Failed to pull log: {e}", file=sys.stderr)

        # 拉取输出文件
        try:
            pull_outputs(case_id, run_dir, target, verbose=verbose)
        except:
            pass

        mark_case_interrupted(case_id, run_dir)
        print(f"[{case_id}] 已保存数据并标记为手动退出")
        return False

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(f"[{case_id}] Error occurred, pulling available data...")

        # Force stop 小艺
        try:
            force_stop(target=target, verbose=verbose)
            print(f"[{case_id}] Force stopped com.huawei.hmos.vassistant")
        except:
            pass

        # 拉取 Desktop/Downloads/Documents 下的输出文件
        print(f"[{case_id}] Pulling output files...")
        pulled = pull_outputs(case_id, run_dir, target, verbose=verbose)
        print(f"[{case_id}] Pulled {len(pulled)} output files")

        mark_case_failed(case_id, run_dir, str(exc))
        print(f"[{case_id}] Marked as failed")
        return False


def _wait_continue_stop(
    *,
    item_id: str,
    before: dict[str, tuple[int, int]],
    target: str | None,
    initial_date_id: str,
    poll_seconds: float,
    timeout_seconds: int,
    verbose: bool = False,
):
    """continue 专用监控：只接受 baseline 之后新出现的 stop_reason=stop。

    与 wait_for_task_done 的区别：
    1. 每轮扫描所有「相对 baseline 增长」的 jsonl，不把监控钉死在单个文件上
      （continue 可能把本次回复追加到任意一个复用的会话文件里，也可能新建文件）；
    2. 只读 baseline 之后的字节（start_byte = base_size + 1），
      避免把复用会话里旧一轮的 stop 误判为本次完成。
    """
    deadline = time.monotonic() + timeout_seconds
    last_wait_print = 0.0
    last_verbose_print = 0.0  # [hdc] 命令详情打印间隔，避免 poll 时刷屏
    verbose_interval = 180
    while time.monotonic() < deadline:
        now = time.monotonic()
        poll_verbose = verbose and (last_verbose_print == 0 or now - last_verbose_print >= verbose_interval)
        logs = list_remote_logs(target=target, user_id=None, date_id=initial_date_id, verbose=poll_verbose)
        for log in logs:
            base_size = before.get(log.path, (0, 0))[0]
            if log.size <= base_size:
                continue  # 相对 baseline 没有增长，本轮无新内容
            text = read_remote_stop_candidates(
                log,
                target=target,
                lines=300,
                start_byte=base_size + 1,
                verbose=poll_verbose,
            )
            if has_stop_reason_stop(text):
                print(f"[{item_id}] New stop found in: {log.path}")
                return log
        if poll_verbose:
            last_verbose_print = now
        if now - last_wait_print >= 300:
            print(f"[{item_id}] Waiting for a NEW stop after baseline ...")
            last_wait_print = now
        time.sleep(poll_seconds)

    raise TaskTimeoutError(
        f"{item_id} timed out waiting for a new stop_reason=stop after baseline.",
        active_log=None,
    )


def run_continue_case(
    dialog_page_id: str | None = None,
    query: str = "继续",
    *,
    config: dict,
    case_id: str,
    run_dir: str,
    target: str | None,
    verbose: bool = False,
) -> bool:
    """
    在已有 dialogPageId 中继续对话，跳过 setup 和 cleanup

    流程：
    1. 从本地 meta.json 读取 remote_log 路径（和 dialog_page_id）
    2. 调用 start_prompt 发送 query（带 historySessionId）
    3. 监控远程日志的 stop_count 变化
    4. 拉取日志
    5. 提取 stop content
    6. 强制停止
    7. 拉取输出文件
    8. 标记完成
    """

    # 继续对话场景：跳过已完成检查，因为用户就是想继续对话

    timeout = config.get('xiaoyi_timeout', 1200)
    poll_seconds = 3.0

    # Step 1: 从本地 meta.json 读取 remote_log 信息
    meta_file = Path(run_dir) / case_id / f"{case_id}.meta.json"
    if not meta_file.exists():
        print(f"[{case_id}] Error: meta.json not found at {meta_file}", file=sys.stderr)
        mark_case_failed(case_id, run_dir, "meta.json not found")
        return False

    import json
    with open(meta_file, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    remote_log_path = meta.get('remote_log', '')
    remote_user_id = meta.get('remote_user_id', '')
    remote_log_name = meta.get('remote_log_name', '')
    # 如果没有传入 dialog_page_id，从 meta.json 读取
    if not dialog_page_id:
        dialog_page_id = meta.get('dialog_page_id', '')

    if not remote_log_path:
        print(f"[{case_id}] Error: remote_log not found in meta.json", file=sys.stderr)
        mark_case_failed(case_id, run_dir, "remote_log not found in meta.json")
        return False

    if not dialog_page_id:
        print(f"[{case_id}] Error: dialog_page_id not found. Please provide it via --continue argument or ensure meta.json contains dialog_page_id", file=sys.stderr)
        mark_case_failed(case_id, run_dir, "dialog_page_id not found")
        return False

    print(f"\n{'='*60}")
    print(f"Continuing case: {case_id} with dialogPageId: {dialog_page_id}")
    print(f"{'='*60}")
    print(f"[{case_id}] Remote log from meta.json: {remote_log_path}")
    print(f"[{case_id}] Remote user_id: {remote_user_id}")

    # 续接会新开/复用会话，实际写入的 jsonl 不一定是 meta.json 里的 remote_log。
    # 因此不再死盯 meta.json 的旧日志，改为推送前打全量快照，推送后只监控
    # 「相对 baseline 增长」的 jsonl，并且只接受 baseline 之后新出现的 stop。

    try:
        # Step 2: 发送 continue 前，对全部 jsonl 打快照作为基准
        current_date_id = today_id()
        before = snapshot(list_remote_logs(
            target=target,
            user_id=None,
            date_id=current_date_id,
            verbose=verbose,
        ))
        print(f"[{case_id}] Baseline log snapshot taken ({len(before)} logs)")

        # Step 3: 启动继续对话
        # 用 start_prompt + historySessionId 继续原对话（pc_agent_task_start + historySessionId）
        # 不要用 continue_start_prompt（pc_agent_task_list_history），那是刷新历史列表用的，会忽略 query
        print(f"[{case_id}] Starting continue prompt...")
        # 保存续接 query 文本到 case 目录，便于事后回查
        save_prompt_text(query, case_id=case_id, run_dir=run_dir, tag="continue")
        start_prompt(query, target=target, verbose=verbose, history_session_id=dialog_page_id)

        # Step 4: 监控「新增或内容变化的 jsonl」在 baseline 之后新出现的 stop_reason=stop
        print(f"[{case_id}] Waiting for task to complete (monitoring new stop after baseline)...")
        done_log = _wait_continue_stop(
            item_id=case_id,
            before=before,
            target=target,
            initial_date_id=current_date_id,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout,
            verbose=verbose,
        )

        print(f"[{case_id}] Task completed, log: {done_log.path}")

        # 等待日志稳定
        time.sleep(1.5)

        # Phase 1/4: 拉取日志
        print(f"[{case_id}] [Phase 1/4] Pulling log...")
        local_log = pull_log(
            done_log,
            case_id=case_id,
            run_dir=run_dir,
            target=target,
            verbose=verbose,
        )
        print(f"[{case_id}] Log pulled to: {local_log}")

        # Phase 2/4: 解析日志，提取 stop content 并输出
        print(f"[{case_id}] [Phase 2/4] Extracting stop content...")
        extract_stop_content(local_log, case_id, run_dir)

        # Phase 3/4: Force stop 小艺
        print(f"[{case_id}] [Phase 3/4] Force stopping...")
        force_stop(target=target, verbose=verbose)
        print(f"[{case_id}] Force stopped com.huawei.hmos.vassistant")

        # Phase 4/4: 拉取输出文件
        print(f"[{case_id}] [Phase 4/4] Pulling output files...")
        pulled = pull_outputs(case_id, run_dir, target, verbose=verbose)
        print(f"[{case_id}] Pulled {len(pulled)} output files")

        # 标记完成
        mark_case_completed(case_id, run_dir)
        print(f"Case {case_id} continued successfully")
        return True

    except TaskTimeoutError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
        print(f"[{case_id}] Task timeout, pulling available data...")

        force_stop(target=target, verbose=verbose)

        if exc.active_log is not None:
            try:
                print(f"[{case_id}] [Phase 1/4] Pulling log...")
                local_log = pull_log(
                    exc.active_log,
                    case_id=case_id,
                    run_dir=run_dir,
                    target=target,
                    verbose=verbose,
                )
                print(f"[{case_id}] Log pulled to: {local_log}")

                print(f"[{case_id}] [Phase 2/4] Extracting stop content...")
                extract_stop_content(local_log, case_id, run_dir)
            except Exception as e:
                print(f"[{case_id}] Failed to pull log: {e}", file=sys.stderr)

        print(f"[{case_id}] [Phase 4/4] Pulling output files...")
        pulled = pull_outputs(case_id, run_dir, target, verbose=verbose)
        print(f"[{case_id}] Pulled {len(pulled)} output files")

        mark_case_failed(case_id, run_dir, str(exc))
        print(f"[{case_id}] Marked as failed (timeout)")
        return False

    except KeyboardInterrupt:
        print(f"\n[{case_id}] 手动中断，正在拉取数据...")

        try:
            force_stop(target=target, verbose=verbose)
        except:
            pass

        try:
            if 'monitor_log' in dir() and monitor_log is not None:
                local_log = pull_log(monitor_log, case_id=case_id, run_dir=run_dir, target=target, verbose=verbose)
                print(f"[{case_id}] Log pulled to: {local_log}")
            else:
                current_logs = list_remote_logs(target=target, user_id=None, date_id=today_id(), verbose=verbose)
                if current_logs:
                    local_log = pull_log(current_logs[0], case_id=case_id, run_dir=run_dir, target=target, verbose=verbose)
                    print(f"[{case_id}] Log pulled to: {local_log}")
        except Exception as e:
            print(f"[{case_id}] Failed to pull log: {e}", file=sys.stderr)

        try:
            pull_outputs(case_id, run_dir, target, verbose=verbose)
        except:
            pass

        mark_case_interrupted(case_id, run_dir)
        print(f"[{case_id}] 已保存数据并标记为手动退出")
        return False

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(f"[{case_id}] Error occurred, pulling available data...")

        try:
            force_stop(target=target, verbose=verbose)
        except:
            pass

        print(f"[{case_id}] Pulling output files...")
        pulled = pull_outputs(case_id, run_dir, target, verbose=verbose)
        print(f"[{case_id}] Pulled {len(pulled)} output files")

        mark_case_failed(case_id, run_dir, str(exc))
        print(f"[{case_id}] Marked as failed")
        return False


def main():
    parser = argparse.ArgumentParser(description='Run test suite orchestration')
    parser.add_argument('--case', '-c',
                        help='Single case ID (e.g., FileOrganization_0_001)')
    parser.add_argument('--batch', '-b', action='store_true',
                        help='Batch mode: run all cases')
    parser.add_argument('--config', default='config.json',
                        help='Config file path (default: config.json)')
    parser.add_argument('--date', '-d',
                        help='Run date for output directory (default: today)')
    parser.add_argument('--output-base',
                        help='Override output root (the run_<date> directory is created below it)')
    parser.add_argument('--skip-setup', action='store_true',
                        help='Skip file setup (assume files already sent)')
    parser.add_argument('--clean', action='store_true',
                        help='Clean remote directories before each case')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')
    parser.add_argument('--log-hdc', action='store_true',
                        help='把每条 hdc 命令（命令行、退出码、耗时、stdout 摘要）追加写到 run_dir/hdc_commands.log')
    parser.add_argument('--cases-list',
                        help='JSON file with case list to run')
    parser.add_argument('--restart-delay', type=float, default=3.0,
                        help='相邻任务间隔秒数，默认 3')
    parser.add_argument('--stop-on-error', action='store_true',
                        help='某条失败后立即停止整个批次')
    parser.add_argument('--continue', dest='continue_dialog', nargs='?', const='', default=None,
                        help='Continue existing dialog with dialogPageId (if not provided, read from meta.json)')
    parser.add_argument('--query', '-q', default='继续',
                        help='Query to send when continuing dialog (default: 继续)')
    args = parser.parse_args()

    # 加载配置
    if os.path.exists(args.config):
        config = load_config(args.config)
    else:
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    test_file_base = config['test_file_base']
    output_base = args.output_base or config.get('output_base', 'test_runs')
    output_base = os.path.abspath(os.path.expanduser(output_base))

    # 确定运行目录
    run_date = args.date or datetime.now().strftime('%Y%m%d')
    run_dir = os.path.join(output_base, f"run_{run_date}")
    os.makedirs(run_dir, exist_ok=True)

    print(f"Run directory: {run_dir}")

    # 可选：开启 hdc 命令日志（写到 run_dir/hdc_commands.log）
    if args.log_hdc:
        hdc_log_path = os.path.join(run_dir, "hdc_commands.log")
        set_hdc_logger(HdcCommandLogger(hdc_log_path))
        print(f"[hdc-log] HDC command log enabled: {hdc_log_path}")

    if args.continue_dialog is not None and args.case:
        # 继续对话模式（优先）
        dialog_page_id = args.continue_dialog
        success = run_continue_case(
            dialog_page_id,
            query=args.query,
            config=config,
            case_id=args.case,
            run_dir=run_dir,
            target=config.get('hdc_target'),
            verbose=args.verbose,
        )
        sys.exit(0 if success else 1)

    elif args.case:
        # 单个case模式
        success = run_single_case(args.case, config, run_dir, verbose=args.verbose, skip_setup=args.skip_setup, clean=args.clean)
        sys.exit(0 if success else 1)

    elif args.batch:
        # 批量模式
        # 读取case列表
        if args.cases_list and os.path.exists(args.cases_list):
            import json
            with open(args.cases_list, 'r', encoding='utf-8') as f:
                case_ids = json.load(f)
        else:
            # 从test_file目录扫描
            case_ids = get_all_cases(test_file_base)

        # 过滤掉已完成的（断点续传）
        pending_cases = [c for c in case_ids if not is_case_completed(c, run_dir)]

        print(f"Total cases: {len(case_ids)}")
        print(f"Completed: {len(case_ids) - len(pending_cases)}")
        print(f"Pending: {len(pending_cases)}")

        if not pending_cases:
            print("All cases already completed!")
            return

        success_count = 0
        fail_count = 0

        try:
            for case_id in pending_cases:
                print(f"\n>>> Progress: {success_count + fail_count + 1}/{len(pending_cases)} <<<")

                success = run_single_case(case_id, config, run_dir, verbose=args.verbose, skip_setup=args.skip_setup, clean=args.clean)

                if success:
                    success_count += 1
                else:
                    fail_count += 1
                    if args.stop_on_error:
                        break

                # 相邻任务间隔延迟
                if case_id != pending_cases[-1] and args.restart_delay > 0:
                    print(f"[{case_id}] 等待 {args.restart_delay:g} 秒后启动下一条 ...")
                    time.sleep(args.restart_delay)

        except KeyboardInterrupt:
            print("\n批量执行被手动中断")

        print(f"\n{'='*60}")
        print(f"Batch complete!")
        print(f"Success: {success_count}")
        print(f"Failed: {fail_count}")
        print(f"{'='*60}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
