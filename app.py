import os
import json
import threading
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'groups.json')
HTML_FILE = os.path.join(BASE_DIR, 'index.html')

# 删除操作所需的鉴权密码，可通过环境变量 DELETE_PASSWORD 覆盖默认值。
# 前端通过 URL 参数 ?password=xxx 传入。
DELETE_PASSWORD = os.environ.get('DELETE_PASSWORD', 'admin123')

# 读、写共用同一把可重入锁：load_data/save_data 自身加锁，
# 而“读取-修改-写回”的调用方（如 add_group）再套一层锁保证整体原子。
# 用 RLock 是因为持锁的调用方需要再次调用会自行加锁的 load_data/save_data。
lock = threading.RLock()


class DataCorruptedError(Exception):
    """groups.json 存在但无法作为分组列表解析时抛出。

    绝不能把它当成“空数据”返回，否则下一次写入会把全部历史记录静默覆盖。
    """


def _quarantine_corrupted_file():
    """把损坏的数据文件改名隔离（带时间戳），返回给调用方展示的结果说明"""
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = '{}.{}.bak'.format(DATA_FILE, stamp)
    index = 1
    while os.path.exists(backup):
        index += 1
        backup = '{}.{}-{}.bak'.format(DATA_FILE, stamp, index)

    try:
        os.replace(DATA_FILE, backup)
    except OSError:
        return '损坏文件隔离失败，请手动检查 {}'.format(DATA_FILE)

    return '损坏文件已备份为 {}'.format(backup)


def load_data():
    """读取分组数据（加锁，保证读到的是完整内容）

    - 文件不存在：返回 []（首次运行）
    - 文件损坏 / 格式非法：先隔离备份原文件，再抛出 DataCorruptedError
    - 读取失败（权限、占用等）：同样抛出 DataCorruptedError，不伪装成“没有数据”
    """
    with lock:
        if not os.path.exists(DATA_FILE):
            return []

        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise DataCorruptedError(
                '数据文件解析失败（{}）'.format(_quarantine_corrupted_file())
            ) from exc
        except OSError as exc:
            raise DataCorruptedError('数据文件无法读取：{}'.format(exc)) from exc

        if not isinstance(data, list):
            raise DataCorruptedError(
                '数据文件格式异常，顶层应为列表（{}）'.format(_quarantine_corrupted_file())
            )

        return data


def save_data(data):
    """原子写入数据文件

    先写同目录下的临时文件并 fsync 落盘，再用 os.replace 原子替换目标文件，
    避免写入中途失败/崩溃留下被截断的 JSON（那正是数据被清空的源头）。
    临时文件名带 pid，避免多进程部署时互相踩踏。
    """
    with lock:
        tmp_file = '{}.{}.tmp'.format(DATA_FILE, os.getpid())
        try:
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, DATA_FILE)
        except BaseException:
            # 保留原文件不被破坏，清掉可能残留的临时文件
            if os.path.exists(tmp_file):
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass
            raise


# ---------- 前端页面 ----------
@app.route('/')
def index():
    """返回同目录下的 index.html"""
    if os.path.exists(HTML_FILE):
        return send_from_directory(BASE_DIR, 'index.html')
    return (
        '<h3>未找到 index.html</h3>'
        '<p>请将前端页面保存为 index.html，并与 app.py 放在同一目录下。</p>',
        404,
    )


# ---------- API：获取分组列表 ----------
@app.route('/api/groups', methods=['GET'])
def get_groups():
    data = load_data()
    return jsonify({'groups': data})


# ---------- API：新增分组 ----------
@app.route('/api/groups', methods=['POST'])
def add_group():
    if not request.is_json:
        return jsonify({'error': '请求体必须为 JSON 格式'}), 400

    payload = request.get_json(silent=True) or {}
    name1 = (payload.get('name1') or '').strip()
    name2 = (payload.get('name2') or '').strip()

    if not name1 or not name2:
        return jsonify({'error': '请填写两位同学的姓名'}), 400
    if name1 == name2:
        return jsonify({'error': '同一组内两位同学的姓名不能相同'}), 400

    with lock:
        data = load_data()
        new_id = max((item.get('id', 0) for item in data), default=0) + 1
        new_group = {
            'id': new_id,
            'name1': name1,
            'name2': name2,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        data.append(new_group)
        save_data(data)

    return jsonify(new_group), 201


# ---------- API：删除分组 ----------
@app.route('/api/groups/<int:group_id>', methods=['DELETE'])
def delete_group(group_id):
    # 删除需要密码鉴权，密码通过 URL 参数 ?password=xxx 传入
    password = request.args.get('password', '')
    if password != DELETE_PASSWORD:
        return jsonify({'error': '删除密码错误或缺失'}), 401

    with lock:
        data = load_data()
        new_data = [item for item in data if item.get('id') != group_id]

        if len(new_data) == len(data):
            return jsonify({'error': '未找到该分组'}), 404

        save_data(new_data)

    return jsonify({'ok': True, 'deleted_id': group_id}), 200


# ---------- 数据文件损坏：明确报错，不静默降级为空数据 ----------
@app.errorhandler(DataCorruptedError)
def data_corrupted(error):
    app.logger.error('数据文件异常：%s', error)
    return jsonify({
        'error': '数据文件已损坏或无法读取，原文件已自动备份，'
                 '请查看服务端日志后手动恢复，系统已暂停写入以免覆盖历史数据',
    }), 500


# ---------- 统一 JSON 错误响应（API 路径） ----------
@app.errorhandler(404)
def not_found(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': '接口不存在'}), 404
    return error


@app.errorhandler(500)
def server_error(error):
    if request.path.startswith('/api/'):
        return jsonify({'error': '服务器内部错误'}), 500
    return error


if __name__ == '__main__':
    # 0.0.0.0 方便局域网内其他设备访问；调试模式按需开启
    app.run(host='0.0.0.0', port=5000, debug=True)