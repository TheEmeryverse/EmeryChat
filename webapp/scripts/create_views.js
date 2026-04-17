require('../env.js')

const nano = require('nano')(`${process.env.COUCHDB_PROTOCOL}://${process.env.COUCHDB_USERNAME}:${process.env.COUCHDB_PASSWORD}@${process.env.COUCHDB_ADDRESS}:${process.env.COUCHDB_PORT}`);
const dbName = process.env.COUCHDB_NAME;

async function recreateDesignDocs() {
  try {
    const db = nano.db.use(dbName);
    
    // Define the design documents to create
    const designDocs = [
      {
        _id: '_design/user',
        views: {
          forName: {
            map: "function (doc) {\n  if (doc.type === 'user') {\n    emit(doc.name, {\n      _id: doc.id,\n      name: doc.name,\n      last_seen: doc.last_seen\n    })\n  }\n}"
          },
          all: {
            map: "function (doc) {\n  if (doc.type === 'user') {\n    emit(doc._id, {\n      _id: doc._id,\n      name: doc.name,\n      last_seen: doc.last_seen\n    });\n  }\n}"
          }
        },
        language: "javascript"
      }
    ];

    // Delete existing design documents and recreate them
    for (const designDoc of designDocs) {
      try {
        // Get the current design document to check if it exists
        const currentDoc = await db.get(designDoc._id);
        
        // Delete the existing design document
        await db.destroy(designDoc._id, currentDoc._rev);
        console.log(`Deleted existing ${designDoc._id}`);
      } catch (error) {
        if (error.statusCode !== 404) {
          console.error(`Error deleting ${designDoc._id}:`, error);
          throw error;
        }
        // If it doesn't exist, that's fine
        console.log(`${designDoc._id} does not exist, creating fresh`);
      }

      // Create the new design document
      await db.insert(designDoc);
      console.log(`Created new ${designDoc._id}`);
    }

    console.log('All design documents recreated successfully!');
  } catch (error) {
    console.error('Error recreating design documents:', error);
    process.exit(1);
  }
}

// Run the function
recreateDesignDocs();