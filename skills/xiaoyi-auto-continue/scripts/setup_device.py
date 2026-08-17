#!/usr/bin/env python3
"""
setup_device.py - 解析setup.json并发送文件到远程设备

读取指定case的setup.json，解析file_send字段，
生成并执行HDC文件发送命令。
"""

import argparse
import json
import os
import subprocess
import sys


HDC_PATH = "hdc"  # 假设hdc在PATH中

# 路径映射：setup.json 中的路径 -> HDC 实际可访问路径
_FILE_PATH_MAPPINGS = {
    "/data/service/el2/100/hmdfs/account/files/Docs/Desktop": "/storage/media/100/local/files/Docs/Desktop",
    "/data/service/el2/100/hmdfs/account/files/Docs/Download": "/storage/media/100/local/files/Docs/Download",
    "/data/service/el2/100/hmdfs/account/files/Docs/Documents": "/storage/media/100/local/files/Docs/Documents",
}


def map_file_path(remote_path: str) -> str:
    """映射文件路径到 HDC 可访问的实际路径"""
    # 直接用完整路径匹配（setup.json 中的路径已经是目录路径）
    if remote_path in _FILE_PATH_MAPPINGS:
        return _FILE_PATH_MAPPINGS[remote_path]
    return remote_path


def run_hdc(command: str, target: str = None, timeout: int = 30) -> tuple:
    """执行HDC shell命令"""
    cmd = [HDC_PATH]
    if target:
        cmd.extend(['-t', target])
    cmd.extend(['shell', 'sh', '-c', command])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timeout"
    except Exception as e:
        return -1, "", str(e)


def run_hdc_direct(args: list, target: str = None, timeout: int = 60, cwd: str = None) -> tuple:
    """直接执行HDC命令（不加shell前缀，用于file send等）"""
    cmd = [HDC_PATH]
    if target:
        cmd.extend(['-t', target])
    cmd.extend(args)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timeout"
    except Exception as e:
        return -1, "", str(e)


def load_config(config_path: str) -> dict:
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def parse_setup_json(setup_path: str, test_file_base: str) -> list:
    """解析setup.json，返回文件发送任务列表"""
    with open(setup_path, 'r', encoding='utf-8') as f:
        setup = json.load(f)

    tasks = []
    case_dir = os.path.dirname(setup_path)  # .../FileOrganization_0_001/

    for file_send_item in setup.get('file_send', []):
        if len(file_send_item) != 2:
            print(f"Warning: invalid file_send item: {file_send_item}", file=sys.stderr)
            continue

        local_rel, remote_path = file_send_item

        # 映射远程路径（setup.json 路径 -> HDC 实际路径）
        mapped_remote_path = map_file_path(remote_path)

        # 拼接完整本地路径，使用 os.path.abspath 确保路径格式正确
        # 然后转成正斜杠（HDC 能识别）
        local_abs = os.path.abspath(os.path.join(test_file_base, local_rel)).replace('\\', '/')

        # 确保文件存在
        if not os.path.exists(local_abs):
            print(f"Warning: file not found: {local_abs}", file=sys.stderr)
            continue

        tasks.append({
            'local': local_abs,
            'remote': mapped_remote_path,
        })

    return tasks


def check_file_sent(case_id: str, output_base: str) -> bool:
    """检查文件是否已经发送过（断点续传）"""
    marker_file = os.path.join(output_base, case_id, 'setup_done.json')
    return os.path.exists(marker_file)


def mark_file_sent(case_id: str, output_base: str, tasks: list) -> None:
    """标记文件已发送"""
    case_output = os.path.join(output_base, case_id)
    os.makedirs(case_output, exist_ok=True)

    marker = {
        'case_id': case_id,
        'tasks': tasks
    }
    marker_file = os.path.join(case_output, 'setup_done.json')
    with open(marker_file, 'w', encoding='utf-8') as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)


def clean_remote_paths(target: str, setup_json_path: str, test_file_base: str, verbose: bool = False) -> bool:
    """清理远程目录中的文件（只清理 file_send 的目标目录，不清理 check_path）"""
    with open(setup_json_path, 'r', encoding='utf-8') as f:
        setup = json.load(f)

    paths_to_clean = set()

    # 收集 file_send 的目标目录（映射前和映射后都要清理）
    for file_send_item in setup.get('file_send', []):
        if len(file_send_item) >= 2:
            remote_path = file_send_item[1]
            # 映射后的路径
            mapped_path = map_file_path(remote_path)
            if mapped_path != remote_path:
                paths_to_clean.add(mapped_path)
            # 映射前的路径（原始路径）
            paths_to_clean.add(remote_path)

    success = True
    for path in paths_to_clean:
        if verbose:
            print(f"Cleaning: {path}")
        # 执行清理命令：删除目录下所有文件但保留目录
        returncode, stdout, stderr = run_hdc(f'rm -rf {path}/*', target=target, timeout=60)
        if returncode != 0:
            print(f"Warning: Failed to clean {path}: {stderr}", file=sys.stderr)
            # 不算失败，可能目录不存在
        else:
            if verbose:
                print(f"OK: Cleaned {path}")

    return success


def send_files(target: str, tasks: list, verbose: bool = False) -> bool:
    """发送文件到远程设备"""
    success = True
    for task in tasks:
        filename = os.path.basename(task['local'])
        remote_path = task['remote']

        # 目标路径需要包含文件名
        remote_full_path = f"{remote_path}/{filename}"

        # 获取源文件所在目录，切换到该目录后用相对路径执行（解决Windows上HDC路径bug）
        local_dir = os.path.dirname(task['local'])

        # 构建 HDC file send 命令（使用相对路径）
        hdc_args = ['file', 'send', filename, remote_full_path]

        print(f"[HDC] hdc {' '.join(hdc_args)}")

        returncode, stdout, stderr = run_hdc_direct(hdc_args, target=target, cwd=local_dir)

        if returncode != 0:
            print(f"Error sending {task['local']}: {stderr}", file=sys.stderr)
            success = False
        else:
            if verbose:
                print(f"OK: {os.path.basename(task['local'])}")

            # 自动解压 zip 文件
            if task['local'].lower().endswith('.zip'):
                unzip_cmd = f'unzip -o {remote_full_path} -d {remote_path}'
                print(f"[HDC] hdc shell {unzip_cmd}")
                unzip_returncode, unzip_stdout, unzip_stderr = run_hdc(unzip_cmd, target=target)
                if unzip_returncode != 0:
                    print(f"Warning: unzip failed: {unzip_stderr}")
                else:
                    print(f"OK: Unzipped {os.path.basename(task['local'])}")

    return success


def get_all_cases(test_file_base: str) -> list:
    """获取所有测试case列表"""
    cases = []
    for item in os.listdir(test_file_base):
        case_dir = os.path.join(test_file_base, item)
        if os.path.isdir(case_dir) and item.startswith('FileOrganization'):
            setup_json = os.path.join(case_dir, 'setup.json')
            if os.path.exists(setup_json):
                cases.append(item)
    return sorted(cases)


def setup_single_case(case_id: str, config: dict, run_dir: str = None, verbose: bool = False, clean: bool = False) -> bool:
    """为单个case发送文件"""
    test_file_base = config['test_file_base']
    output_base = run_dir if run_dir else config.get('output_base', 'test_runs')
    target = config.get('hdc_target')  # 不设默认值，让 run_hdc 使用 HDC 默认目标

    case_dir = os.path.join(test_file_base, case_id)
    setup_json = os.path.join(case_dir, 'setup.json')

    if not os.path.exists(setup_json):
        print(f"Error: setup.json not found: {setup_json}", file=sys.stderr)
        return False

    # 清理远程目录
    if clean:
        print(f"Cleaning remote paths for {case_id}...")
        clean_remote_paths(target, setup_json, test_file_base, verbose=verbose)

    # 解析setup.json
    tasks = parse_setup_json(setup_json, test_file_base)
    if not tasks:
        print(f"No files to send for {case_id}")
        return True

    # 检查是否已发送（清理后需要重新发送）
    if not clean and check_file_sent(case_id, output_base):
        print(f"Files already sent for {case_id} (skipping)")
        return True

    # 发送文件
    print(f"Setting up {case_id}...")
    success = send_files(target, tasks, verbose=verbose)

    if success:
        mark_file_sent(case_id, output_base, tasks)
        print(f"Setup complete for {case_id}")

    return success


def main():
    parser = argparse.ArgumentParser(description='Setup device by sending files from setup.json')
    parser.add_argument('--case', '-c',
                        help='Single case ID (e.g., FileOrganization_0_001)')
    parser.add_argument('--batch', '-b', action='store_true',
                        help='Batch mode: setup all cases')
    parser.add_argument('--config', default='config.json',
                        help='Config file path (default: config.json)')
    parser.add_argument('--target', '-t',
                        help='HDC target (overrides config)')
    parser.add_argument('--clean', action='store_true',
                        help='Clean remote directories before sending files')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')
    args = parser.parse_args()

    # 加载配置
    if os.path.exists(args.config):
        config = load_config(args.config)
    else:
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    if args.target:
        config['hdc_target'] = args.target

    test_file_base = config['test_file_base']

    if args.batch:
        # 批量模式
        cases = get_all_cases(test_file_base)
        print(f"Found {len(cases)} cases")

        success_count = 0
        for case_id in cases:
            if setup_single_case(case_id, config, verbose=args.verbose, clean=args.clean):
                success_count += 1

        print(f"\nBatch complete: {success_count}/{len(cases)} cases successful")

    elif args.case:
        # 单个case模式
        success = setup_single_case(args.case, config, verbose=args.verbose, clean=args.clean)
        sys.exit(0 if success else 1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
