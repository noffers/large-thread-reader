import os
import time
import datetime
from flask import Flask, render_template, request, jsonify
import praw
from praw.models import Submission
from prawcore import NotFound, TooManyRequests, Forbidden, ResponseException
import sqlite3
from threading import Thread, Lock
import json
from collections import defaultdict
import traceback
import logging

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Initialize Reddit instance
reddit = praw.Reddit(
    client_id=REDDIT_CLIENT_ID,
    client_secret=REDDIT_CLIENT_SECRET,
    user_agent=REDDIT_USER_AGENT
)

# Database setup
DB_PATH = 'reddit_comments.db'
db_lock = Lock()

# Thread status tracking
thread_status = {}
status_lock = Lock()

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
            last_fetched INTEGER,
            fetch_status TEXT,
            error_message TEXT
        )
    ''')
    
    # Add new columns if they don't exist
    try:
        c.execute('ALTER TABLE threads ADD COLUMN fetch_status TEXT DEFAULT "success"')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    try:
        c.execute('ALTER TABLE threads ADD COLUMN error_message TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    conn.commit()
    conn.close()

def update_thread_status(thread_id, status, error_message=None):
    """Update thread fetching status"""
    logger.info(f"update_thread_status called: thread_id={thread_id}, status={status}, error={error_message}")
    
    try:
        with status_lock:
            thread_status[thread_id] = {
                'status': status,
                'error': error_message,
                'timestamp': time.time()
            }
            logger.info(f"Updated in-memory status for {thread_id}: {thread_status[thread_id]}")
        
        # Also update database - use INSERT OR REPLACE to ensure record exists
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # First, try to update existing record
            c.execute('''
                UPDATE threads 
                SET fetch_status = ?, error_message = ?
                WHERE id = ?
            ''', (status, error_message, thread_id))
            rows_updated = c.rowcount
            
            # If no rows were updated, insert a minimal record
            if rows_updated == 0:
                logger.info(f"No existing thread record for {thread_id}, creating one")
                c.execute('''
                    INSERT INTO threads (id, title, url, last_fetched, fetch_status, error_message)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (thread_id, '', '', int(time.time()), status, error_message))
                rows_updated = c.rowcount
            
            conn.commit()
            conn.close()
            logger.info(f"Updated database status for {thread_id}: {rows_updated} rows affected")
            
    except Exception as e:
        logger.error(f"Error in update_thread_status for {thread_id}: {e}")
        logger.error(traceback.format_exc())

def fetch_comments(thread_id, force_refresh=False):
    """Fetch all comments from a Reddit thread with enhanced error handling"""
    # Set status to fetching IMMEDIATELY
    update_thread_status(thread_id, 'fetching')
    logger.info(f"Status set to 'fetching' for thread {thread_id}")
    
    try:
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
                    logger.info(f"Using cached data for thread {thread_id}")
                    update_thread_status(thread_id, 'success')
                    conn.close()
                    return
            
            conn.close()
        
        logger.info(f"Fetching comments for thread {thread_id}")
        
        # Test Reddit API connection first
        try:
            submission: Submission = reddit.submission(id=thread_id)
            # Access a property to trigger API call
            title = submission.title
            logger.info(f"Successfully connected to Reddit API. Thread: {title}")
        except Forbidden as e:
            error_msg = "Reddit API access forbidden. Check your API credentials."
            logger.error(f"Reddit API Forbidden: {e}")
            update_thread_status(thread_id, 'error', error_msg)
            return
        except TooManyRequests as e:
            error_msg = "Reddit API rate limit exceeded. Please wait and try again later."
            logger.error(f"Reddit API Rate Limited: {e}")
            update_thread_status(thread_id, 'error', error_msg)
            return
        except ResponseException as e:
            error_msg = f"Reddit API error: {str(e)}"
            logger.error(f"Reddit API Response Error: {e}")
            update_thread_status(thread_id, 'error', error_msg)
            return
        except Exception as e:
            error_msg = f"Failed to connect to Reddit API: {str(e)}"
            logger.error(f"Reddit API Connection Error: {e}")
            update_thread_status(thread_id, 'error', error_msg)
            return
        
        # Replace more comments with retry logic
        max_retries = 3
        retry_count = 0
        
        logger.info(f"Starting to replace more comments for thread {thread_id}")
        while retry_count < max_retries:
            try:
                logger.info(f"Replacing more comments (attempt {retry_count + 1}/{max_retries})")
                submission.comments.replace_more(limit=32)
                logger.info(f"Successfully replaced more comments for thread {thread_id}")
                break
            except TooManyRequests as e:
                retry_count += 1
                if retry_count >= max_retries:
                    error_msg = "Reddit API rate limit exceeded during comment fetching. Please wait and try again."
                    logger.error(f"Rate limited during replace_more: {e}")
                    update_thread_status(thread_id, 'error', error_msg)
                    return
                
                # Wait before retry (exponential backoff)
                wait_time = 2 ** retry_count
                logger.warning(f"Rate limited, waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
            except Exception as e:
                retry_count += 1
                logger.error(f"Error during replace_more (attempt {retry_count}/{max_retries}): {e}")
                if retry_count >= max_retries:
                    error_msg = f"Error fetching comments after {max_retries} attempts: {str(e)}"
                    logger.error(f"Failed to replace more comments: {e}")
                    update_thread_status(thread_id, 'error', error_msg)
                    return
                
                wait_time = 2 ** retry_count
                logger.warning(f"Error during replace_more, waiting {wait_time} seconds before retry: {e}")
                time.sleep(wait_time)
        
        # Process all comments
        logger.info(f"Starting to process comments for thread {thread_id}")
        comments_data = []
        comment_count = 0
        
        try:
            for comment in submission.comments.list():
                if isinstance(comment, praw.models.MoreComments):
                    continue
                
                comment_count += 1
                
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
                            if depth > 10:  # Prevent infinite loops
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
                
                # Log progress for large threads
                if comment_count % 1000 == 0:
                    logger.info(f"Processed {comment_count} comments...")
            
            logger.info(f"Finished processing {comment_count} comments for thread {thread_id}")
            
        except Exception as e:
            error_msg = f"Error processing comments: {str(e)}"
            logger.error(f"Error processing comments for thread {thread_id}: {e}")
            logger.error(traceback.format_exc())
            update_thread_status(thread_id, 'error', error_msg)
            return
        
        # Store in database
        logger.info(f"Starting database storage for thread {thread_id} with {len(comments_data)} comments")
        try:
            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                
                try:
                    # Store thread info
                    logger.info(f"Storing thread info for {thread_id}")
                    c.execute('''
                        INSERT OR REPLACE INTO threads (id, title, url, last_fetched, fetch_status, error_message)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (thread_id, submission.title, submission.url, int(time.time()), 'success', None))
                    
                    # Clear old comments for this thread
                    logger.info(f"Clearing old comments for thread {thread_id}")
                    c.execute('DELETE FROM comments WHERE thread_id = ?', (thread_id,))
                    
                    # Bulk insert comments
                    if comments_data:
                        logger.info(f"Inserting {len(comments_data)} comments for thread {thread_id}")
                        c.executemany('''
                            INSERT INTO comments 
                            (id, thread_id, parent_id, author, body, score, created_utc, is_root, depth, permalink, last_updated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', comments_data)
                    
                    conn.commit()
                    logger.info(f"Database commit successful for thread {thread_id}")
                    
                    # CRITICAL: Update status to success AFTER successful database commit
                    logger.info(f"About to update status to 'success' for thread {thread_id}")
                    try:
                        update_thread_status(thread_id, 'success')
                        logger.info(f"Status successfully updated to 'success' for thread {thread_id}")
                    except Exception as status_error:
                        logger.error(f"Failed to update status to success for thread {thread_id}: {status_error}")
                        logger.error(traceback.format_exc())
                        # Even if status update fails, we successfully stored comments
                        # So manually update database
                        try:
                            c.execute('UPDATE threads SET fetch_status = ? WHERE id = ?', ('success', thread_id))
                            conn.commit()
                            logger.info(f"Manually updated database status to success for thread {thread_id}")
                        except Exception as db_status_error:
                            logger.error(f"Failed to manually update database status: {db_status_error}")
                    
                    logger.info(f"Successfully stored {len(comments_data)} comments for thread {thread_id}")
                    
                except Exception as e:
                    error_msg = f"Database error: {str(e)}"
                    logger.error(f"Database error for thread {thread_id}: {e}")
                    logger.error(traceback.format_exc())
                    update_thread_status(thread_id, 'error', error_msg)
                finally:
                    conn.close()
                    
        except Exception as e:
            error_msg = f"Database connection error: {str(e)}"
            logger.error(f"Database connection error for thread {thread_id}: {e}")
            logger.error(traceback.format_exc())
            update_thread_status(thread_id, 'error', error_msg)
            
    except NotFound:
        error_msg = f"Reddit thread {thread_id} not found. Please check the URL."
        logger.error(error_msg)
        update_thread_status(thread_id, 'error', error_msg)
    except Exception as e:
        error_msg = f"Unexpected error fetching comments: {str(e)}"
        logger.error(f"Unexpected error for thread {thread_id}: {e}")
        logger.error(traceback.format_exc())
        update_thread_status(thread_id, 'error', error_msg)

def get_comments_filtered(thread_id, hours=24, sort_by='score', min_score=1):
    """Get filtered comments from the database"""
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
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
            
            c.execute(query, (thread_id, time_threshold, min_score))
            rows = c.fetchall()
            comments = []
            for row in rows:
                comment = dict(row)
                # Ensure boolean conversion
                comment['is_root'] = bool(comment['is_root'])
                comments.append(comment)
            
            # Get thread info including status
            c.execute('SELECT * FROM threads WHERE id = ?', (thread_id,))
            thread_row = c.fetchone()
            thread_info = dict(thread_row) if thread_row else None
            
            return comments, thread_info
        except Exception as e:
            logger.error(f"Error in get_comments_filtered: {str(e)}")
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

@app.route('/health_check')
def health_check():
    """Health check endpoint to test Reddit API connectivity"""
    try:
        # Test with a real Reddit post that should exist
        test_submission = reddit.submission(id='3hahrw')  # A well-known test post
        title = test_submission.title  # This will test if we can make API calls
        
        return jsonify({
            'status': 'healthy',
            'reddit_api': 'connected',
            'message': f'Reddit API credentials are working. Test post: "{title[:50]}..."',
            'client_id': REDDIT_CLIENT_ID[:8] + '...' if len(REDDIT_CLIENT_ID) > 8 else 'not_set'
        })
    except Forbidden:
        return jsonify({
            'status': 'error',
            'reddit_api': 'forbidden', 
            'message': 'Reddit API access forbidden - check your client ID and secret',
            'client_id': REDDIT_CLIENT_ID[:8] + '...' if len(REDDIT_CLIENT_ID) > 8 else 'not_set'
        }), 403
    except Exception as e:
        return jsonify({
            'status': 'error',
            'reddit_api': 'error',
            'message': f'Reddit API error: {str(e)}',
            'client_id': REDDIT_CLIENT_ID[:8] + '...' if len(REDDIT_CLIENT_ID) > 8 else 'not_set'
        }), 500

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
    
    # Set status to fetching BEFORE starting the thread
    update_thread_status(thread_id, 'fetching')
    logger.info(f"Set status to 'fetching' for thread {thread_id} before starting background thread")
    
    # Start fetching in background
    Thread(target=fetch_comments, args=(thread_id, True)).start()
    
    return jsonify({'thread_id': thread_id, 'status': 'fetching'})

@app.route('/fix_status/<thread_id>', methods=['POST'])
def fix_thread_status(thread_id):
    """Fix thread status if comments exist but status is stuck on fetching"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Check if comments exist
            c.execute('SELECT COUNT(*) as comment_count FROM comments WHERE thread_id = ?', (thread_id,))
            count_row = c.fetchone()
            comment_count = count_row[0] if count_row else 0
            
            if comment_count > 0:
                # Comments exist, update status to success
                c.execute('UPDATE threads SET fetch_status = ? WHERE id = ?', ('success', thread_id))
                conn.commit()
                
                # Also update in-memory status
                with status_lock:
                    thread_status[thread_id] = {
                        'status': 'success',
                        'error': None,
                        'timestamp': time.time()
                    }
                
                logger.info(f"Manually fixed status for thread {thread_id} with {comment_count} comments")
                conn.close()
                
                return jsonify({
                    'status': 'fixed',
                    'message': f'Status updated to success for thread with {comment_count} comments'
                })
            else:
                conn.close()
                return jsonify({
                    'status': 'no_comments',
                    'message': 'No comments found in database for this thread'
                }), 400
                
    except Exception as e:
        logger.error(f"Error fixing status for thread {thread_id}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/debug_thread/<thread_id>')
def debug_thread(thread_id):
    """Debug endpoint to check thread state"""
    try:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            # Get thread info
            c.execute('SELECT * FROM threads WHERE id = ?', (thread_id,))
            thread_row = c.fetchone()
            thread_info = dict(thread_row) if thread_row else None
            
            # Get comment count
            c.execute('SELECT COUNT(*) as comment_count FROM comments WHERE thread_id = ?', (thread_id,))
            count_row = c.fetchone()
            comment_count = count_row['comment_count'] if count_row else 0
            
            # Get sample comments
            c.execute('SELECT id, author, score, created_utc FROM comments WHERE thread_id = ? LIMIT 5', (thread_id,))
            sample_comments = [dict(row) for row in c.fetchall()]
            
            conn.close()
            
            # Get in-memory status
            with status_lock:
                memory_status = thread_status.get(thread_id, {'status': 'not_found'})
            
            return jsonify({
                'thread_id': thread_id,
                'thread_info': thread_info,
                'comment_count': comment_count,
                'sample_comments': sample_comments,
                'memory_status': memory_status
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/thread_status/<thread_id>')
def get_thread_status(thread_id):
    """Get the current status of thread fetching"""
    with status_lock:
        memory_status = thread_status.get(thread_id, None)
    
    db_status = None
    # Also check database for persistent status
    try:
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('SELECT fetch_status, error_message, last_fetched FROM threads WHERE id = ?', (thread_id,))
            row = c.fetchone()
            conn.close()
            
            if row:
                db_status = {
                    'status': row['fetch_status'] or 'unknown',
                    'error': row['error_message'],
                    'last_fetched': row['last_fetched']
                }
                
    except Exception as e:
        logger.error(f"Error getting thread status from database: {e}")
    
    # Decide which status to return - prioritize memory if it's more recent and valid
    if memory_status and memory_status.get('status') in ['fetching', 'success', 'error']:
        # Memory status exists and is valid
        if db_status and db_status.get('status') == 'success' and memory_status.get('status') == 'fetching':
            # Database shows success but memory shows fetching - sync them
            logger.info(f"Syncing status: DB shows success for {thread_id}, updating memory")
            with status_lock:
                thread_status[thread_id] = {
                    'status': 'success',
                    'error': None,
                    'timestamp': time.time()
                }
            return jsonify({'status': 'success', 'error': None, 'timestamp': time.time()})
        else:
            # Use memory status (it's more current)
            return jsonify(memory_status)
    elif db_status:
        # No valid memory status, use database
        return jsonify(db_status)
    else:
        # Nothing found
        return jsonify({'status': 'unknown', 'error': None, 'timestamp': time.time()})

@app.route('/get_comments/<thread_id>')
def get_comments(thread_id):
    """Get filtered comments for a thread"""
    try:
        hours = float(request.args.get('hours', 24))
        sort_by = request.args.get('sort', 'score')
        min_score = int(request.args.get('min_score', 1))
        
        comments, thread_info = get_comments_filtered(thread_id, hours, sort_by, min_score)
        
        if not thread_info:
            return jsonify({'error': 'Thread not found or not yet fetched'}), 404
        
        # Check if there was an error during fetching
        if thread_info.get('fetch_status') == 'error':
            error_msg = thread_info.get('error_message', 'Unknown error occurred during fetching')
            return jsonify({'error': error_msg}), 500
        
        # Build comment tree
        comment_tree = build_comment_tree(comments)
        
        return jsonify({
            'thread_info': thread_info,
            'comments': comment_tree,
            'total_comments': len(comments),
            'fetch_status': thread_info.get('fetch_status', 'unknown')
        })
    except Exception as e:
        logger.error(f"Error in get_comments: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': f'Failed to load comments: {str(e)}'}), 500

@app.route('/refresh_thread/<thread_id>', methods=['POST'])
def refresh_thread(thread_id):
    """Refresh comments for a thread"""
    # Set status to fetching BEFORE starting the thread
    update_thread_status(thread_id, 'fetching')
    logger.info(f"Set status to 'fetching' for thread {thread_id} before starting refresh")
    
    Thread(target=fetch_comments, args=(thread_id, True)).start()
    return jsonify({'status': 'fetching'})

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