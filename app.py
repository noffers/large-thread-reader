import os
import time
import datetime
from flask import Flask, render_template, request, jsonify
import praw
from prawcore import NotFound
import sqlite3
from threading import Thread, Lock
import json
from collections import defaultdict

app = Flask(__name__)

# Reddit API credentials - set these as environment variables
REDDIT_CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID', 'your_client_id')
REDDIT_CLIENT_SECRET = os.environ.get('REDDIT_CLIENT_SECRET', 'your_client_secret')
REDDIT_USER_AGENT = os.environ.get('REDDIT_USER_AGENT', 'RedditCommentViewer/1.0')

# Initialize Reddit instance
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT
)

# Database setup
DB_PATH = 'reddit_comments.db'
db_lock = Lock()

def init_db():
    """Initialize the SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            parent_id TEXT,
            author TEXT,
            body TEXT,
            score INTEGER,
            created_utc INTEGER,
            is_root BOOLEAN,
            depth INTEGER,
            permalink TEXT,
            last_updated INTEGER
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            last_fetched INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def fetch_comments(thread_id, force_refresh=False):
    """Fetch all comments from a Reddit thread"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # Check if we need to refresh
        c.execute('SELECT last_fetched FROM threads WHERE id = ?', (thread_id,))
        result = c.fetchone()
        
        if result and not force_refresh:
            last_fetched = result[0]
            # If fetched within last 5 minutes, use cache
            if time.time() - last_fetched < 300:
                conn.close()
                return
        
        try:
            submission = reddit.submission(id=thread_id)
            submission.comments.replace_more(limit=None)
            
            # Store thread info
            c.execute('''
                INSERT OR REPLACE INTO threads (id, title, url, last_fetched)
                VALUES (?, ?, ?, ?)
            ''', (thread_id, submission.title, submission.url, int(time.time())))
            
            # Clear old comments for this thread
            c.execute('DELETE FROM comments WHERE thread_id = ?', (thread_id,))
            
            # Process all comments
            comments_data = []
            for comment in submission.comments.list():
                if isinstance(comment, praw.models.MoreComments):
                    continue
                
                # Determine if root comment
                is_root = comment.parent_id.startswith('t3_')
                parent_id = None if is_root else comment.parent_id[3:]
                
                # Calculate depth
                depth = 0
                if not is_root:
                    temp_comment = comment
                    while hasattr(temp_comment, 'parent') and not temp_comment.parent_id.startswith('t3_'):
                        depth += 1
                        try:
                            temp_comment = temp_comment.parent()
                        except:
                            break
                
                comments_data.append((
                    comment.id,
                    thread_id,
                    parent_id,
                    str(comment.author) if comment.author else '[deleted]',
                    comment.body,
                    comment.score,
                    int(comment.created_utc),
                    is_root,
                    depth,
                    comment.permalink,
                    int(time.time())
                ))
            
            # Bulk insert comments
            c.executemany('''
                INSERT INTO comments 
                (id, thread_id, parent_id, author, body, score, created_utc, is_root, depth, permalink, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', comments_data)
            
            conn.commit()
            print(f"Fetched {len(comments_data)} comments for thread {thread_id}")
            
        except NotFound:
            print(f"Thread {thread_id} not found")
        except Exception as e:
            print(f"Error fetching comments: {e}")
        finally:
            conn.close()

def get_comments_filtered(thread_id, hours=24, sort_by='score', min_score=1):
    """Get filtered comments from the database"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Calculate time threshold
        time_threshold = int(time.time()) - (hours * 3600)
        
        # Build query based on sort criteria
        base_query = '''
            SELECT * FROM comments 
            WHERE thread_id = ? 
            AND created_utc >= ?
            AND score >= ?
        '''
        
        if sort_by == 'score':
            query = base_query + ' ORDER BY score DESC, created_utc DESC'
        elif sort_by == 'time':
            query = base_query + ' ORDER BY created_utc DESC'
        elif sort_by == 'controversial':
            # Simple controversial: low score but high activity (replies)
            query = base_query + ' ORDER BY score ASC, created_utc DESC'
        else:
            query = base_query + ' ORDER BY score DESC'
        
        c.execute(query, (thread_id, time_threshold, min_score))
        comments = [dict(row) for row in c.fetchall()]
        
        # Get thread info
        c.execute('SELECT * FROM threads WHERE id = ?', (thread_id,))
        thread_info = dict(c.fetchone()) if c.fetchone() else None
        
        conn.close()
        return comments, thread_info

def build_comment_tree(comments):
    """Build a tree structure from flat comment list"""
    comment_dict = {c['id']: c for c in comments}
    roots = []
    
    for comment in comments:
        comment['children'] = []
        if comment['is_root']:
            roots.append(comment)
        elif comment['parent_id'] in comment_dict:
            comment_dict[comment['parent_id']]['children'].append(comment)
    
    return roots

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/fetch_thread', methods=['POST'])
def fetch_thread():
    """Endpoint to fetch a Reddit thread"""
    data = request.json
    thread_url = data.get('thread_url', '')
    
    # Extract thread ID from URL
    thread_id = None
    if '/comments/' in thread_url:
        parts = thread_url.split('/comments/')
        if len(parts) > 1:
            thread_id = parts[1].split('/')[0]
    
    if not thread_id:
        return jsonify({'error': 'Invalid thread URL'}), 400
    
    # Start fetching in background
    Thread(target=fetch_comments, args=(thread_id, True)).start()
    
    return jsonify({'thread_id': thread_id, 'status': 'fetching'})

@app.route('/get_comments/<thread_id>')
def get_comments(thread_id):
    """Get filtered comments for a thread"""
    hours = int(request.args.get('hours', 24))
    sort_by = request.args.get('sort', 'score')
    min_score = int(request.args.get('min_score', 1))
    
    comments, thread_info = get_comments_filtered(thread_id, hours, sort_by, min_score)
    
    if not thread_info:
        return jsonify({'error': 'Thread not found'}), 404
    
    # Build comment tree
    comment_tree = build_comment_tree(comments)
    
    return jsonify({
        'thread_info': thread_info,
        'comments': comment_tree,
        'total_comments': len(comments)
    })

@app.route('/refresh_thread/<thread_id>', methods=['POST'])
def refresh_thread(thread_id):
    """Refresh comments for a thread"""
    Thread(target=fetch_comments, args=(thread_id, True)).start()
    return jsonify({'status': 'refreshing'})

@app.template_filter('format_time')
def format_time(timestamp):
    """Format Unix timestamp to readable time"""
    return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

@app.template_filter('time_ago')
def time_ago(timestamp):
    """Convert timestamp to 'X hours ago' format"""
    now = time.time()
    diff = now - timestamp
    
    if diff < 60:
        return f"{int(diff)} seconds ago"
    elif diff < 3600:
        return f"{int(diff/60)} minutes ago"
    elif diff < 86400:
        return f"{int(diff/3600)} hours ago"
    else:
        return f"{int(diff/86400)} days ago"

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=8080)