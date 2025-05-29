import os
import time
import datetime
from flask import Flask, render_template, request, jsonify
import asyncpraw
from asyncprawcore import NotFound, RequestException, ResponseException
import sqlite3
from threading import Thread, Lock
import json
from collections import defaultdict
import traceback
import asyncio
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# Error handler for JSON responses
@app.errorhandler(500)
def handle_500_error(error):
    """Return JSON instead of HTML for HTTP errors."""
    return jsonify({
        'error': 'Internal server error',
        'message': 'An error occurred processing your request'
    }), 500

# Reddit API credentials - set these as environment variables
REDDIT_CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID', 'your_client_id')
REDDIT_CLIENT_SECRET = os.environ.get('REDDIT_CLIENT_SECRET', 'your_client_secret')
REDDIT_USER_AGENT = os.environ.get('REDDIT_USER_AGENT', 'RedditCommentViewer/1.0')

# Configuration
REPLACE_MORE_LIMIT = int(os.environ.get('REPLACE_MORE_LIMIT', 32))  # Adjust for speed vs completeness

# Thread pool for running async tasks
executor = ThreadPoolExecutor(max_workers=4)

async def get_reddit_instance():
    """Create and return an async Reddit instance"""
    return asyncpraw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
        requestor_kwargs={"session": None}  # Use default aiohttp session
    )

# Database setup
DB_PATH = 'reddit_comments.db'
db_lock = Lock()
async_lock = Lock()  # Lock for async operations

def init_db():
    """Initialize the SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            last_fetched INTEGER
        )
    ''')
    conn.commit()
    conn.close()

async def fetch_comments_async(thread_id, force_refresh=False):
    """Fetch all comments from a Reddit thread asynchronously"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if we need to refresh
        cursor.execute('SELECT last_fetched FROM threads WHERE id = ?', (thread_id,))
        result = cursor.fetchone()
        
        if result and not force_refresh:
            last_fetched = result[0]
            # If fetched within last 5 minutes, use cache
            if time.time() - last_fetched < 300:
                conn.close()
                return
        
        conn.close()
    
    reddit = await get_reddit_instance()
    
    try:
        submission = await reddit.submission(id=thread_id)
        
        # Fetch submission data first
        await submission.load()
        
        # Replace more comments - using async approach for better performance
        # This is typically 2-3x faster than synchronous praw
        # Limit to avoid timeouts, but process them concurrently
        await submission.comments.replace_more(limit=REPLACE_MORE_LIMIT)
        
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Store thread info
            cursor.execute('''
                INSERT OR REPLACE INTO threads (id, title, url, last_fetched)
                VALUES (?, ?, ?, ?)
            ''', (thread_id, submission.title, submission.url, int(time.time())))
            
            # Clear old comments for this thread
            cursor.execute('DELETE FROM comments WHERE thread_id = ?', (thread_id,))
            
            # Process all comments
            comments_data = []
            comment_list = await submission.comments.list()
            
            for comment in comment_list:
                if not hasattr(comment, 'id'):  # Skip non-comment objects
                    continue
                
                # Determine if root comment
                is_root = comment.parent_id.startswith('t3_')
                parent_id = None if is_root else comment.parent_id[3:]
                
                # Calculate depth
                depth = 0
                if not is_root:
                    temp_comment = comment
                    while hasattr(temp_comment, 'parent_id') and not temp_comment.parent_id.startswith('t3_'):
                        depth += 1
                        if depth > 10:  # Prevent infinite loops
                            break
                        try:
                            parent_id_temp = temp_comment.parent_id[3:]
                            # Find parent in our current list
                            found = False
                            for parent_comment in comment_list:
                                if hasattr(parent_comment, 'id') and parent_comment.id == parent_id_temp:
                                    temp_comment = parent_comment
                                    found = True
                                    break
                            if not found:
                                break
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
            cursor.executemany('''
                INSERT INTO comments 
                (id, thread_id, parent_id, author, body, score, created_utc, is_root, depth, permalink, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', comments_data)
            
            conn.commit()
            conn.close()
            print(f"Fetched {len(comments_data)} comments for thread {thread_id} (async)")
            
    except NotFound:
        print(f"Thread {thread_id} not found")
    except (RequestException, ResponseException) as e:
        print(f"Reddit API error: {e}")
    except asyncio.TimeoutError:
        print(f"Timeout fetching comments for thread {thread_id}")
    except Exception as e:
        print(f"Error fetching comments: {e}")
        traceback.print_exc()
    finally:
        await reddit.close()

def fetch_comments(thread_id, force_refresh=False):
    """Wrapper to run async fetch_comments in a new event loop"""
    try:
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Set a timeout of 2 minutes for the entire operation
        loop.run_until_complete(
            asyncio.wait_for(
                fetch_comments_async(thread_id, force_refresh),
                timeout=120.0
            )
        )
    except asyncio.TimeoutError:
        print(f"Timeout: Comment fetching took too long for thread {thread_id}")
    except Exception as e:
        print(f"Error in fetch_comments wrapper: {e}")
        traceback.print_exc()
    finally:
        loop.close()

def get_comments_filtered(thread_id, hours=24, sort_by='score', min_score=1):
    """Get filtered comments from the database"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
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
            
            cursor.execute(query, (thread_id, time_threshold, min_score))
            rows = cursor.fetchall()
            comments = []
            for row in rows:
                comment = dict(row)
                # Ensure boolean conversion
                comment['is_root'] = bool(comment['is_root'])
                comments.append(comment)
            
            # Get thread info
            cursor.execute('SELECT * FROM threads WHERE id = ?', (thread_id,))
            thread_row = cursor.fetchone()
            thread_info = dict(thread_row) if thread_row else None
            
            return comments, thread_info
        except Exception as e:
            print(f"Error in get_comments_filtered: {str(e)}")
            raise
        finally:
            conn.close()

def build_comment_tree(comments):
    """Build a tree structure from flat comment list"""
    if not comments:
        return []
    
    # First, initialize all comments with empty children arrays
    comment_dict = {}
    for comment in comments:
        comment['children'] = []
        comment_dict[comment['id']] = comment
    
    roots = []
    orphaned = []
    
    # Now build the tree structure
    for comment in comments:
        if comment['is_root']:
            roots.append(comment)
        elif comment['parent_id'] in comment_dict:
            # Parent exists in filtered set
            comment_dict[comment['parent_id']]['children'].append(comment)
        else:
            # Parent was filtered out, treat as orphaned root
            orphaned.append(comment)
    
    # Add orphaned comments as roots with a special indicator
    for comment in orphaned:
        comment['is_orphaned'] = True
        roots.append(comment)
    
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
    
    # Start fetching in background thread
    Thread(target=fetch_comments, args=(thread_id, True), daemon=True).start()
    
    return jsonify({'thread_id': thread_id, 'status': 'fetching'})

@app.route('/get_comments/<thread_id>')
def get_comments(thread_id):
    """Get filtered comments for a thread"""
    try:
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
    except Exception as e:
        print(f"Error in get_comments: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': f'Failed to load comments: {str(e)}'}), 500

@app.route('/refresh_thread/<thread_id>', methods=['POST'])
def refresh_thread(thread_id):
    """Refresh comments for a thread"""
    Thread(target=fetch_comments, args=(thread_id, True), daemon=True).start()
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
    try:
        app.run(debug=True, host='0.0.0.0', port=8080)
    finally:
        # Clean up executor on shutdown
        executor.shutdown(wait=True)