"""Lua CAS scripts for atomic board task operations."""

# claim_task: Atomically claim a task if not already claimed.
# KEYS[1] = ws:board:claims:{board_id}
# ARGV[1] = task_id
# ARGV[2] = pane_id
# ARGV[3] = epoch timestamp
# Returns: 1 = success, 0 = already claimed
CLAIM_TASK_LUA = """
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then return 0 end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode({
    pane = ARGV[2], claimed_at = tonumber(ARGV[3])
}))
return 1
"""

# drop_task: Release a claimed task (only the claimer can drop).
# KEYS[1] = ws:board:claims:{board_id}
# ARGV[1] = task_id
# ARGV[2] = pane_id (must match current claimer)
# Returns: 1 = success, 0 = not claimed or wrong pane
DROP_TASK_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local data = cjson.decode(raw)
if data.pane ~= ARGV[2] then return 0 end
redis.call('HDEL', KEYS[1], ARGV[1])
return 1
"""
